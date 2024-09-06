"""ATProto firehose client. Enqueues receive tasks for events for bridged users."""
from collections import namedtuple
from datetime import datetime, timedelta
from io import BytesIO
import itertools
import logging
import os
from queue import Queue
from threading import Event, Lock, Thread, Timer
import threading
import time

from arroba.datastore_storage import AtpRepo
from arroba.util import parse_at_uri
from carbox import read_car
import dag_cbor
import dag_json
from google.cloud import ndb
from granary.bluesky import AT_URI_PATTERN
from lexrpc.client import Client
import libipld
from oauth_dropins.webutil import util
from oauth_dropins.webutil.appengine_config import ndb_client
from oauth_dropins.webutil.appengine_info import DEBUG
from oauth_dropins.webutil.util import json_dumps, json_loads

from atproto import ATProto, Cursor
from common import (
    add,
    cache_policy,
    create_task,
    global_cache,
    global_cache_policy,
    global_cache_timeout_policy,
    report_exception,
    USER_AGENT,
)
from models import Object, reset_protocol_properties

logger = logging.getLogger(__name__)

RECONNECT_DELAY = timedelta(seconds=30)
STORE_CURSOR_FREQ = timedelta(seconds=10)

# a commit operation. similar to arroba.repo.Write. record is None for deletes.
Op = namedtuple('Op', ['action', 'repo', 'path', 'seq', 'record'],
                # record is optional
                defaults=[None])

# contains bytes, websocket frames
#
# maxsize is important here! if we hit this limit, subscribe will block when it
# tries to add more commits until handle consumes some. this keeps subscribe
# from getting too far ahead of handle and using too much memory in this queue.
events = Queue(maxsize=200)

# global so that subscribe can reuse it across calls
cursor = None

# global: _load_dids populates them, subscribe and handle use them
atproto_dids = set()
atproto_loaded_at = datetime(1900, 1, 1)
bridged_dids = set()
bridged_loaded_at = datetime(1900, 1, 1)
dids_initialized = Event()


def load_dids():
    # run in a separate thread since it needs to make its own NDB
    # context when it runs in the timer thread
    Thread(target=_load_dids).start()
    dids_initialized.wait()
    dids_initialized.clear()


def _load_dids():
    global atproto_dids, atproto_loaded_at, bridged_dids, bridged_loaded_at

    with ndb_client.context(cache_policy=cache_policy, global_cache=global_cache,
                            global_cache_policy=global_cache_policy,
                            global_cache_timeout_policy=global_cache_timeout_policy):
        if not DEBUG:
            Timer(STORE_CURSOR_FREQ.total_seconds(), _load_dids).start()

        atproto_query = ATProto.query(ATProto.enabled_protocols != None,
                                      ATProto.updated > atproto_loaded_at)
        atproto_loaded_at = ATProto.query().order(-ATProto.updated).get().updated
        new_atproto = [key.id() for key in atproto_query.iter(keys_only=True)]
        atproto_dids.update(new_atproto)

        bridged_query = AtpRepo.query(AtpRepo.status == None,
                                      AtpRepo.created > bridged_loaded_at)
        bridged_loaded_at = AtpRepo.query().order(-AtpRepo.created).get().created
        new_bridged = [key.id() for key in bridged_query.iter(keys_only=True)]
        bridged_dids.update(new_bridged)

        dids_initialized.set()
        total = len(atproto_dids) + len(bridged_dids)
        logger.info(f'DIDs: {total} ATProto {len(atproto_dids)} (+{len(new_atproto)}), AtpRepo {len(bridged_dids)} (+{len(new_bridged)}); events {events.qsize()}')


def subscriber():
    """Wrapper around :func:`_subscribe` that catches exceptions and reconnects."""
    logger.info(f'started thread to subscribe to {os.environ["BGS_HOST"]} firehose')
    load_dids()

    while True:
        try:
            with ndb_client.context(
                    cache_policy=cache_policy, global_cache=global_cache,
                    global_cache_policy=global_cache_policy,
                    global_cache_timeout_policy=global_cache_timeout_policy):
                subscribe()

            logger.info(f'disconnected! waiting {RECONNECT_DELAY} and then reconnecting')
            time.sleep(RECONNECT_DELAY.total_seconds())

        except BaseException:
            report_exception()


def subscribe():
    """Subscribes to the relay's firehose.

    Relay hostname comes from the ``BGS_HOST`` environment variable.

    Args:
      reconnect (bool): whether to always reconnect after we get disconnected
    """
    global cursor
    if not cursor:
        cursor = Cursor.get_or_insert(
            f'{os.environ["BGS_HOST"]} com.atproto.sync.subscribeRepos')
        # TODO: remove? does this make us skip events? if we remove it, will we
        # infinite loop when we fail on an event?
        if cursor.cursor:
            cursor.cursor += 1

    client = Client(f'https://{os.environ["BGS_HOST"]}',
                    headers={'User-Agent': USER_AGENT})

    for frame in client.com.atproto.sync.subscribeRepos(cursor=cursor.cursor):
        # ops = ' '.join(f'{op.get("action")} {op.get("path")}'
        #                for op in payload.get('ops', []))
        # logger.info(f'seeing {payload.get("seq")} {repo} {ops}')

        events.put(frame)


def handler():
    """Wrapper around :func:`handle` that catches exceptions and restarts."""
    logger.info(f'started handle thread to store objects and enqueue receive tasks')

    while True:
        try:
            with ndb_client.context(
                    cache_policy=cache_policy, global_cache=global_cache,
                    global_cache_policy=global_cache_policy,
                    global_cache_timeout_policy=global_cache_timeout_policy):
                handle()

            # if we return cleanly, that means we hit the limit
            break

        except BaseException:
            report_exception()


def handle(limit=None):
    seen = 0

    def _handle(op):
        type, _ = op.path.strip('/').split('/', maxsplit=1)
        record_json = dag_json.encode(op.record, dialect='atproto')
        if type not in ATProto.SUPPORTED_RECORD_TYPES:
            logger.info(f'Skipping unsupported type {type}: {record_json}[:1000]')
            return

        at_uri = f'at://{op.repo}/{op.path}'

        # store object, enqueue receive task
        if op.action in ('create', 'update'):
            if created_at := op.record.get('createdAt'):
                logger.info(f'record createdAt {created_at}')
            record_kwarg = {
                'bsky': json_loads(record_json),
            }
            obj_id = at_uri
        elif op.action == 'delete':
            verb = ('delete'
                    if type in ('app.bsky.actor.profile', 'app.bsky.feed.post')
                    else 'undo')
            obj_id = f'{at_uri}#{verb}'
            record_kwarg = {'our_as1': {
                'objectType': 'activity',
                'verb': verb,
                'id': obj_id,
                'actor': op.repo,
                'object': at_uri,
            }}
        else:
            logger.error(f'Unknown action {action} for {op.repo} {op.path}')
            return

        try:
            obj = Object.get_or_create(id=obj_id, authed_as=op.repo, status='new',
                                       users=[ATProto(id=op.repo).key],
                                       source_protocol=ATProto.LABEL, **record_kwarg)
            create_task(queue='receive', obj=obj.key.urlsafe(), authed_as=op.repo)
            # when running locally, comment out above and uncomment this
            # logger.info(f'enqueuing receive task for {at_uri}')
        except BaseException:
            report_exception()

        nonlocal seen
        seen += 1
        if limit is not None and seen >= limit:
            return

    while frame := events.get():
        header, payload = libipld.decode_dag_cbor_multi(frame)
        buf = BytesIO(frame)

        # parse header
        if header.get('op') == -1:
            logger.warning(f'Got error from relay! {payload}')
            continue

        t = header.get('t')
        if t != '#commit':
            if t not in ('#account', '#identity', '#handle'):
                logger.info(f'Got {t} from relay')
            continue

        # parse payload
        repo = payload.get('repo')
        if not repo:
            logger.warning(f'Payload missing repo! {payload}')
            continue

        seq = payload.get('seq')
        if not seq:
            logger.warning(f'Payload missing seq! {payload}')
            continue

        # if we fail processing this commit and raise an exception up to subscriber,
        # skip it and start with the next commit when we're restarted
        cursor.cursor = seq + 1

        if (threading.current_thread().name == 'atproto_firehose.handler-0'
            and util.now().replace(tzinfo=None) - cursor.updated > STORE_CURSOR_FREQ):
            # it's been long enough, update our stored cursor
            logger.info(f'updating stored cursor to {cursor.cursor}')
            cursor.put()
            # when running locally, comment out put above and uncomment this
            # cursor.updated = util.now().replace(tzinfo=None)

        if payload['repo'] in bridged_dids:
            logger.info(f'Ignoring record from our non-ATProto bridged user {payload["repo"]}')
            continue

        blocks = {}
        if block_bytes := payload.get('blocks'):
           # _, blocks = read_car(block_bytes)
           #  blocks = {block.cid: block for block in blocks}
            _, blocks = libipld.decode_car(block_bytes)

        # detect records that reference an ATProto user, eg replies, likes,
        # reposts, mentions
        for p_op in payload.get('ops', []):
            op = Op(repo=payload['repo'], action=p_op.get('action'),
                    path=p_op.get('path'), seq=payload['seq'])
            if not op.action or not op.path:
                logger.info(
                    f'bad payload! seq {op.seq} has action {op.action} path {op.path}!')
                continue

            is_ours = op.repo in atproto_dids
            if is_ours and op.action == 'delete':
                logger.info(f'Got delete from our ATProto user: {op}')
                # TODO: also detect deletes of records that *reference* our bridged
                # users, eg a delete of a follow or like or repost of them.
                # not easy because we need to getRecord the record to check
                _handle(op)
                continue

            cid = p_op.get('cid')
            block = blocks.get(cid)
            # our own commits are sometimes missing the record
            # https://github.com/snarfed/bridgy-fed/issues/1016
            if not cid or not block:
                continue

            try:
                # op = Op(*op[:-1], record=block)
                op = Op(*op[:-1], record=block)
            except BaseException:
                # https://github.com/hashberg-io/dag-cbor/issues/14
                logger.error(f"Couldn't decode block {cid} seq {op.seq}",
                             exc_info=True)
                continue

            type = op.record.get('$type')
            if not type:
                logger.warning('commit record missing $type! {op.action} {op.repo} {op.path} {cid}')
                logger.warning(dag_json.encode(op.record).decode())
                continue

            if is_ours:
                logger.info(f'Got one from our ATProto user: {op.action} {op.repo} {op.path}')
                _handle(op)
                continue

            subjects = []

            def maybe_add(did_or_ref):
                if isinstance(did_or_ref, dict):
                    match = AT_URI_PATTERN.match(did_or_ref['uri'])
                    if match:
                        did = match.group('repo')
                    else:
                        return
                else:
                    did = did_or_ref

                if did and did in bridged_dids:
                    add(subjects, did)

            if type in ('app.bsky.feed.like', 'app.bsky.feed.repost'):
                maybe_add(op.record['subject'])

            elif type in ('app.bsky.graph.block', 'app.bsky.graph.follow'):
                maybe_add(op.record['subject'])

            elif type == 'app.bsky.feed.post':
                # replies
                if reply := op.record.get('reply'):
                    for ref in 'parent', 'root':
                        maybe_add(reply[ref])

                # mentions
                for facet in op.record.get('facets', []):
                    for feature in facet['features']:
                        if feature['$type'] == 'app.bsky.richtext.facet#mention':
                            maybe_add(feature['did'])

                # quote posts
                if embed := op.record.get('embed'):
                    if embed['$type'] == 'app.bsky.embed.record':
                        maybe_add(embed['record'])
                    elif embed['$type'] == 'app.bsky.embed.recordWithMedia':
                        maybe_add(embed['record']['record'])

            if subjects:
                logger.info(f'Got one re our ATProto users {subjects}: {op.action} {op.repo} {op.path}')
                _handle(op)

    assert False, "handle thread shouldn't reach here!"
