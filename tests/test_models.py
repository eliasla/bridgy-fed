# coding=utf-8
"""Unit tests for models.py."""
from unittest import mock

from arroba.mst import dag_cbor_cid
from Crypto.PublicKey import ECC
from flask import g, get_flashed_messages
from granary import as2
from granary.tests.test_bluesky import ACTOR_PROFILE_BSKY
from multiformats import CID
from oauth_dropins.webutil.testutil import NOW, requests_response

from flask_app import app
import common
from models import AtpNode, Follower, Object, OBJECT_EXPIRE_AGE, User
import protocol

from .test_activitypub import ACTOR
from .testutil import Fake, TestCase


class UserTest(TestCase):

    def setUp(self):
        super().setUp()
        self.request_context.push()
        g.user = self.make_user('y.z')

    def tearDown(self):
        self.request_context.pop()
        super().tearDown()

    def test_get_or_create(self):
        user = Fake.get_or_create('a.b')

        assert not user.direct
        assert user.mod
        assert user.public_exponent
        assert user.private_exponent
        assert user.p256_key

        # check that we can load the keys
        assert user.public_pem()
        assert user.private_pem()

        p256_key = ECC.import_key(user.p256_key)
        assert isinstance(p256_key, ECC.EccKey)
        self.assertEqual('NIST P-256', p256_key.curve)

        # direct should get set even if the user exists
        same = Fake.get_or_create('a.b', direct=True)
        user.direct = True
        self.assert_entities_equal(same, user, ignore=['updated'])

    def test_get_or_create_use_instead(self):
        user = Fake.get_or_create('a.b')
        user.use_instead = g.user.key
        user.put()

        self.assertEqual('y.z', Fake.get_or_create('a.b').key.id())

    def test_href(self):
        href = g.user.href()
        self.assertTrue(href.startswith('data:application/magic-public-key,RSA.'), href)
        self.assertIn(g.user.mod, href)
        self.assertIn(g.user.public_exponent, href)

    def test_public_pem(self):
        pem = g.user.public_pem()
        self.assertTrue(pem.decode().startswith('-----BEGIN PUBLIC KEY-----\n'), pem)
        self.assertTrue(pem.decode().endswith('-----END PUBLIC KEY-----'), pem)

    def test_private_pem(self):
        pem = g.user.private_pem()
        self.assertTrue(pem.decode().startswith('-----BEGIN RSA PRIVATE KEY-----\n'), pem)
        self.assertTrue(pem.decode().endswith('-----END RSA PRIVATE KEY-----'), pem)

    def test_user_page_path(self):
        self.assertEqual('/web/y.z', g.user.user_page_path())
        self.assertEqual('/web/y.z/followers', g.user.user_page_path('followers'))
        self.assertEqual('/fake/foo', self.make_user('foo', cls=Fake).user_page_path())

    def test_user_page_link(self):
        self.assertEqual('<a class="h-card u-author" href="/web/y.z"><img src="" class="profile"> y.z</a>', g.user.user_page_link())
        g.user.actor_as2 = ACTOR
        self.assertEqual('<a class="h-card u-author" href="/web/y.z"><img src="https://user.com/me.jpg" class="profile"> Mrs. ☕ Foo</a>', g.user.user_page_link())

    def test_address(self):
        self.assertEqual('@y.z@y.z', g.user.address())

        g.user.actor_as2 = {'type': 'Person'}
        self.assertEqual('@y.z@y.z', g.user.address())

        g.user.actor_as2 = {'url': 'http://foo'}
        self.assertEqual('@y.z@y.z', g.user.address())

        g.user.actor_as2 = {'url': ['http://foo', 'acct:bar@foo', 'acct:baz@y.z']}
        self.assertEqual('@baz@y.z', g.user.address())

        g.user.direct = False
        self.assertEqual('@y.z@localhost', g.user.address())

    def test_actor_id(self):
        self.assertEqual('http://localhost/y.z', g.user.actor_id())

        g.user.direct = False
        self.assertEqual('http://localhost/r/https://y.z/', g.user.actor_id())


class ObjectTest(TestCase):
    def setUp(self):
        super().setUp()
        self.request_context.push()
        g.user = None

    def tearDown(self):
        self.request_context.pop()
        super().tearDown()

    def test_proxy_url(self):
        obj = Object(id='abc', source_protocol='bluesky')
        self.assertEqual('http://localhost/convert/bluesky/web/abc',
                         obj.proxy_url())

        obj = Object(id='ab#c', source_protocol='ui')
        self.assertEqual('http://localhost/convert/ui/web/ab^^c',
                         obj.proxy_url())

    def test_put(self):
        with self.assertRaises(AssertionError):
            Object(id='x^^y').put()

    def test_get_by_id(self):
        self.assertIsNone(Object.get_by_id('abc'))
        self.assertIsNone(Object.get_by_id('ab^^c'))

        obj = Object(id='abc')
        obj.put()
        self.assertIsNotNone(obj, Object.get_by_id('abc'))

        obj = Object(id='ab#c')
        obj.put()
        self.assert_entities_equal(obj, Object.get_by_id('ab^^c'))

    def test_actor_link(self):
        for expected, as2 in (
                ('href="">', {}),
                ('href="http://foo">foo', {'actor': 'http://foo'}),
                ('href="">Alice', {'actor': {'name': 'Alice'}}),
                ('href="http://foo/">Alice', {'actor': {
                    'name': 'Alice',
                    'url': 'http://foo',
                }}),
                ("""\
        title="Alice">
          <img class="profile" src="http://pic/" />
          Alice""", {'actor': {
            'name': 'Alice',
            'icon': {'type': 'Image', 'url': 'http://pic'},
        }}),
        ):
            obj = Object(id='x', as2=as2)
            self.assert_multiline_in(expected, obj.actor_link())

    def test_actor_link_user(self):
        g.user = Fake(id='user.com', actor_as2={"name": "Alice"})
        obj = Object(id='x', source_protocol='ui', domains=['user.com'])
        self.assertIn(
            'href="/fake/user.com"><img src="" class="profile"> Alice</a>',
            obj.actor_link())

    def test_put_updates_load_cache(self):
        obj = Object(id='x', as2={})
        obj.put()
        self.assert_entities_equal(obj, protocol.objects_cache['x'])

    def test_put_fragment_id_doesnt_update_load_cache(self):
        obj = Object(id='x#y', as2={})
        obj.put()
        self.assertNotIn('x#y', protocol.objects_cache)
        self.assertNotIn('x', protocol.objects_cache)

    def test_computed_properties_without_as1(self):
        Object(id='a').put()

    def test_expire(self):
        obj = Object(id='a', our_as1={'objectType': 'activity', 'verb': 'update'})
        self.assertEqual(NOW + OBJECT_EXPIRE_AGE, obj.expire)

    def test_put_adds_removes_activity_label(self):
        obj = Object(id='x#y', our_as1={})
        obj.put()
        self.assertEqual([], obj.labels)

        obj.our_as1 = {'objectType': 'activity'}
        obj.put()
        self.assertEqual(['activity'], obj.labels)

        obj.labels = ['user']
        obj.put()
        self.assertEqual(['user', 'activity'], obj.labels)

        obj.labels = ['activity', 'user']
        obj.put()
        self.assertEqual(['activity', 'user'], obj.labels)

        obj.our_as1 = {'foo': 'bar'}
        obj.put()
        self.assertEqual(['user'], obj.labels)


class FollowerTest(TestCase):

    def setUp(self):
        super().setUp()
        self.inbound = Follower(dest='user.com', src='http://mas.to/@baz',
                                last_follow={'actor': ACTOR})
        self.outbound = Follower(dest='http://mas.to/@baz', src='user.com',
                                 last_follow={'object': ACTOR})

    def test_to_as1(self):
        self.assertEqual({}, Follower().to_as1())

        as1_actor = as2.to_as1(ACTOR)
        self.assertEqual(as1_actor, self.inbound.to_as1())
        self.assertEqual(as1_actor, self.outbound.to_as1())

    def test_to_as2(self):
        self.assertIsNone(Follower().to_as2())
        self.assertEqual(ACTOR, self.inbound.to_as2())
        self.assertEqual(ACTOR, self.outbound.to_as2())


class AtpNodeTest(TestCase):

    def test_create(self):
        AtpNode.create(ACTOR_PROFILE_BSKY)
        stored = AtpNode.get_by_id(dag_cbor_cid(ACTOR_PROFILE_BSKY).encode('base32'))
        self.assertEqual(ACTOR_PROFILE_BSKY, stored.data)
