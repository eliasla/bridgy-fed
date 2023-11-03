"""Unit tests for ids.py."""
from activitypub import ActivityPub
from atproto import ATProto
from ids import translate_handle, translate_object_id, translate_user_id
from models import Target
from web import Web
from .testutil import Fake, TestCase


class IdsTest(TestCase):
    def test_translate_user_id(self):
        Web(id='user.com', atproto_did='did:plc:123',
            copies=[Target(uri='did:plc:123', protocol='atproto')]).put()
        ActivityPub(id='https://inst/user', atproto_did='did:plc:456',
                    copies=[Target(uri='did:plc:456', protocol='atproto')]).put()
        Fake(id='fake:user', atproto_did='did:plc:789',
             copies=[Target(uri='did:plc:789', protocol='atproto')]).put()

        for from_, id, to, expected in [
            (ActivityPub, 'https://inst/user', ActivityPub, 'https://inst/user'),
            (ActivityPub, 'https://inst/user', ATProto, 'did:plc:456'),
            (ActivityPub, 'https://inst/user', Fake, 'fake:u:https://inst/user'),
            (ActivityPub, 'https://inst/user', Web, 'https://inst/user'),
            (ATProto, 'did:plc:123', Web, 'user.com'),
            (ATProto, 'did:plc:456', ActivityPub, 'https://inst/user'),
            (ATProto, 'did:plc:456', ATProto, 'did:plc:456'),
            (ATProto, 'did:plc:789', Fake, 'fake:user'),
            (Fake, 'fake:user', ActivityPub, 'https://fa.brid.gy/ap/fake:user'),
            (Fake, 'fake:user', ATProto, 'did:plc:789'),
            (Fake, 'fake:user', Fake, 'fake:user'),
            (Fake, 'fake:user', Web, 'fake:user'),
            (Web, 'user.com', ActivityPub, 'https://fed.brid.gy/user.com'),
            (Web, 'https://user.com/', ActivityPub, 'https://fed.brid.gy/user.com'),
            (Web, 'user.com', ATProto, 'did:plc:123'),
            (Web, 'https://user.com', ATProto, 'did:plc:123'),
            (Web, 'user.com', Fake, 'fake:u:user.com'),
            (Web, 'user.com', Web, 'user.com'),
            (Web, 'https://user.com/', Web, 'user.com'),
        ]:
            with self.subTest(from_=from_.LABEL, to=to.LABEL):
                self.assertEqual(expected, translate_user_id(
                    id=id, from_proto=from_, to_proto=to))

    def test_translate_user_id_no_copy_did_stored(self):
        for proto, id in [
            (Web, 'user.com'),
            (ActivityPub, 'https://instance/user'),
            (Fake, 'fake:user'),
        ]:
            with self.subTest(proto=proto.LABEL):
                self.assertIsNone(translate_user_id(
                    id=id, from_proto=proto, to_proto=ATProto))
                self.assertIsNone(translate_user_id(
                    id='did:plc:123', from_proto=ATProto, to_proto=proto))

    def test_translate_handle(self):
        for from_, handle, to, expected in [
            # basic
            (Web, 'user.com', ActivityPub, '@user.com@web.brid.gy'),
            (Web, 'user.com', ATProto, 'user.com.web.brid.gy'),
            (Web, 'user.com', Fake, 'fake:handle:user.com'),
            (Web, 'user.com', Web, 'user.com'),
            # # TODO: enhanced
            # (Web, 'user.com', ActivityPub, '@user.com@user.com'),
            # (Web, 'user.com', Fake, 'fake:handle:user.com'),

            # TODO: webfinger lookup
            (ActivityPub, '@user@instance', ActivityPub, '@user@instance'),
            (ActivityPub, '@user@instance', ATProto, 'user.instance.ap.brid.gy'),
            (ActivityPub, '@user@instance', Fake, 'fake:handle:@user@instance'),
            (ActivityPub, '@user@instance', Web, 'instance/@user'),
            # # # TODO: enhanced
            # (ActivityPub, '@user@instance', Web, 'https://instance/user'),
            # (ActivityPub, '@user@instance', Fake,
            #  'fake:handle:https://instance/user'),

            (ATProto, 'user.com', ActivityPub, '@user.com@atproto.brid.gy'),
            (ATProto, 'user.com', ATProto, 'user.com'),
            (ATProto, 'user.com', Fake, 'fake:handle:user.com'),
            (ATProto, 'user.com', Web, 'user.com'),
            # # # TODO: enhanced
            # (ATProto, '@user@instance', ActivityPub, 'user.com'),

            (Fake, 'fake:handle:user', ActivityPub, '@fake:handle:user@fa.brid.gy'),
            (Fake, 'fake:handle:user', ATProto, 'fake:handle:user.fa.brid.gy'),
            (Fake, 'fake:handle:user', Fake, 'fake:handle:user'),
            (Fake, 'fake:handle:user', Web, 'fake:handle:user'),
        ]:
            with self.subTest(from_=from_.LABEL, to=to.LABEL):
                self.assertEqual(expected, translate_handle(
                    handle=handle, from_proto=from_, to_proto=to))

    def test_translate_object_id(self):
        self.store_object(id='http://post',
                          copies=[Target(uri='at://did/web/post', protocol='atproto')])
        self.store_object(id='https://inst/post',
                          copies=[Target(uri='at://did/ap/post', protocol='atproto')])
        self.store_object(id='fake:post',
                          copies=[Target(uri='at://did/fa/post', protocol='atproto')])

        for from_, id, to, expected in [
            (ActivityPub, 'https://inst/post', ActivityPub, 'https://inst/post'),
            (ActivityPub, 'https://inst/post', ATProto, 'at://did/ap/post'),
            (ActivityPub, 'https://inst/post', Fake, 'fake:o:ap:https://inst/post'),
            (ActivityPub, 'https://inst/post',
             Web, 'https://ap.brid.gy/convert/web/https:/inst/post'),
            (ATProto, 'at://did/web/post', Web, 'http://post'),
            (ATProto, 'at://did/ap/post', ActivityPub, 'https://inst/post'),
            (ATProto, 'at://did/atp/post', ATProto, 'at://did/atp/post'),
            (ATProto, 'at://did/fa/post', Fake, 'fake:post'),
            (Fake, 'fake:post',
             ActivityPub, 'https://fa.brid.gy/convert/ap/fake:post'),
            (Fake, 'fake:post', ATProto, 'at://did/fa/post'),
            (Fake, 'fake:post', Fake, 'fake:post'),
            (Fake, 'fake:post', Web, 'https://fa.brid.gy/convert/web/fake:post'),
            (Web, 'http://post',
             ActivityPub, 'https://web.brid.gy/convert/ap/http:/post'),
            (Web, 'http://post', ATProto, 'at://did/web/post'),
            (Web, 'http://post', Fake, 'fake:o:web:http://post'),
            (Web, 'http://post', Web, 'http://post'),
        ]:
            with self.subTest(from_=from_.LABEL, to=to.LABEL):
                self.assertEqual(expected, translate_object_id(
                    id=id, from_proto=from_, to_proto=to))
