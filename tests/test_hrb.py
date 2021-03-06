import hashlib

from twisted.trial.unittest import TestCase
from twisted.internet import reactor
from twisted.internet.defer import inlineCallbacks, returnValue, Deferred
from twisted.web.server import Site

from hrb.bouncer import BounceResource
from hrb.handlers.base import BaseHandler
from hrb.handlers.wurfl_handler.simple import SimpleWurflHandler
from hrb.utils import http


class TestHandler(BaseHandler):
    def __init__(self, callback):
        self.callback = callback

    def setup_handler(self):
        d = Deferred()
        reactor.callLater(0, d.callback, self)
        return d

    def teardown_handler(self):
        pass

    def handle_request(self, request):
        return self.callback(request)


class FakeMemcached(object):
    def __init__(self):
        self._data = {}
        self._calls = {}

    def set(self, key, value, expireTime=0):
        self._data[key] = (value, expireTime)

    def get(self, key):
        if key in self._data:
            call_counter = self._calls.setdefault(key, 0)
            call_counter += 1
            self._calls[key] = call_counter
            value, _ = self._data.get(key)
            return 0, value
        return 0, None

    def increment(self, key, value):
        int_val = int(self._data[key][0])
        int_val += int(value)
        self.set(key, str(int_val))
        return str(int_val)

    def __contains__(self, key):
        return key in self._data

    def times_called(self, key):
        return self._calls.get(key)

    def key_lifetime(self, key):
        value, life_time = self._data.get(key)
        return life_time


class HrbTestCase(TestCase):

    timeout = 1

    def setUp(self):
        self._running_handlers = []
        self.header_handlers = self.setup_handlers(map(TestHandler, [
            lambda request: request.setHeader('X-UA-Type', 'small'),
            lambda request: request.setHeader('X-UA-Category', 'mobi'),
        ]))

        self.body_handlers = self.setup_handlers(map(TestHandler, [
            lambda request: 'foo',
            lambda request: 'bar',
        ]))

        self.cookie_handlers = self.setup_handlers(map(TestHandler, [
            lambda request: request.addCookie('UA-Foo', 'bar')
        ]))

        self.fake_memcached = FakeMemcached()
        self.patch(SimpleWurflHandler, 'connect_to_memcached',
            self.patch_memcached)

        self.nokia_ua = 'Nokia3100/1.0 (02.70) Profile/MIDP-1.0 ' \
                            'Configuration/CLDC-1.0'

        self.iphone_ua = 'Mozilla/5.0 (iPhone; U; CPU iPhone OS 2_2_1 ' \
                            'like Mac OS X; en-us) AppleWebKit/525.18.1 ' \
                            '(KHTML, like Gecko) Version/3.1.1 Mobile/5H11 ' \
                            'Safari/525.20'

    def patch_memcached(self, **config):
        return self.fake_memcached

    def setup_handlers(self, handlers):
        return [handler.setup_handler() for handler in handlers]

    def get_wurfl_handler(self):
        return SimpleWurflHandler({
            'cookie_name': 'X-UA-header',
            'cache_prefix': 'prefix',
            'cache_prefix_delimiter': '_',
            'cache_lifetime': 100,
            'debug_path': '/_debug',
        }).setup_handler()

    @inlineCallbacks
    def start_handlers(self, handlers):
        bouncer = BounceResource(handlers)
        handlers = yield bouncer.handlers_ready
        site_factory = Site(bouncer)
        port = reactor.listenTCP(0, site_factory)
        addr = port.getHost()
        url = "http://%s:%s/" % (addr.host, addr.port)
        self._running_handlers.append((port, handlers))
        returnValue((bouncer, url))

    @inlineCallbacks
    def tearDown(self):
        for port, handlers in self._running_handlers:
            for handler in handlers:
                yield handler.teardown_handler()
            port.loseConnection()

    @inlineCallbacks
    def test_response_headers(self):
        bouncer, url = yield self.start_handlers(self.header_handlers)
        response = yield http.request(url)
        self.assertEqual(response.delivered_body, '')
        self.assertEqual(response.headers.getRawHeaders('X-UA-Type'),
                            ['small'])
        self.assertEqual(response.headers.getRawHeaders('X-UA-Category'),
                            ['mobi'])

    @inlineCallbacks
    def test_response_body(self):
        bouncer, url = yield self.start_handlers(self.body_handlers)
        response = yield http.request(url)
        self.assertEqual(response.delivered_body, 'foobar')

    @inlineCallbacks
    def test_response_cookies(self):
        bouncer, url = yield self.start_handlers(self.cookie_handlers)
        response = yield http.request(url)
        self.assertEqual(response.headers.getRawHeaders('Set-Cookie'),
            ['UA-Foo=bar'])

    @inlineCallbacks
    def test_wurfl_nokia_lookup(self):
        wurfl_handler = self.get_wurfl_handler()
        bouncer, url = yield self.start_handlers([wurfl_handler])
        handler = yield wurfl_handler
        response = yield http.request(url, headers={
            'User-Agent': self.nokia_ua,
            })
        self.assertEqual(response.headers.getRawHeaders('Set-Cookie'),
                ['%s=medium' % handler.cookie_name])

    @inlineCallbacks
    def test_wurfl_iphone_lookup(self):
        wurfl_handler = self.get_wurfl_handler()
        bouncer, url = yield self.start_handlers([wurfl_handler])
        handler = yield wurfl_handler
        response = yield http.request(url, headers={
            'User-Agent': self.iphone_ua,
            })
        self.assertEqual(response.headers.getRawHeaders('Set-Cookie'),
                ['%s=high' % handler.cookie_name])

    @inlineCallbacks
    def test_caching_prefix(self):
        wurfl_handler = self.get_wurfl_handler()
        bouncer, url = yield self.start_handlers([wurfl_handler])
        handler = yield wurfl_handler
        cache_key = handler.get_cache_key(self.iphone_ua)
        namespace_version = yield handler.get_namespace_version()
        self.assertEqual(cache_key,
            'prefix_%s_%s' % (
                namespace_version,
                hashlib.md5(self.iphone_ua).hexdigest()
            )
        )

    @inlineCallbacks
    def test_cache_clearing(self):
        wurfl_handler = self.get_wurfl_handler()
        bouncer, url = yield self.start_handlers([wurfl_handler])
        handler = yield wurfl_handler
        namespace_version = yield handler.get_namespace_version()
        self.assertEqual(namespace_version, '0')
        yield handler.increment_namespace()
        namespace_version = yield handler.get_namespace_version()
        self.assertEqual(namespace_version, '1')

    @inlineCallbacks
    def test_caching_wurl_check(self):
        wurfl_handler = self.get_wurfl_handler()
        bouncer, url = yield self.start_handlers([wurfl_handler])
        handler = yield wurfl_handler
        yield http.request(url, headers={
            'User-Agent': self.iphone_ua,
            })
        cache_key = handler.get_cache_key(self.iphone_ua)
        self.assertTrue(cache_key in self.fake_memcached)

        response = yield http.request(url, headers={
            'User-Agent': self.iphone_ua,
            })

        self.assertEqual(self.fake_memcached.times_called(cache_key), 1)
        self.assertTrue(self.fake_memcached.get(cache_key))
        self.assertEqual(response.headers.getRawHeaders('Set-Cookie'),
            ['%s=high' % handler.cookie_name])

    @inlineCallbacks
    def test_redirect_after_cookie_is_set(self):
        wurfl_handler = self.get_wurfl_handler()
        bouncer, url = yield self.start_handlers([wurfl_handler])
        request_path = "some/random/path?true=1"
        response = yield http.request('%s%s' % (url, request_path),
            headers={
                'User-Agent': self.iphone_ua,
            })
        self.assertEqual(response.code, 302)
        self.assertEqual(response.headers.getRawHeaders('Location'),
            ['/%s' % (request_path,)])

    @inlineCallbacks
    def test_cache_lifetime(self):
        wurfl_handler = self.get_wurfl_handler()
        bouncer, url = yield self.start_handlers([wurfl_handler])
        handler = yield wurfl_handler
        request_path = "some/random/path?true=1"
        response = yield http.request('%s%s' % (url, request_path),
            headers={
                'User-Agent': self.iphone_ua,
            })
        self.assertEqual(response.code, 302)
        cache_key = handler.get_cache_key(self.iphone_ua)
        self.assertEqual(self.fake_memcached.key_lifetime(cache_key), 100)

    @inlineCallbacks
    def test_debug_path(self):
        wurfl_handler = self.get_wurfl_handler()
        bouncer, url = yield self.start_handlers([wurfl_handler])
        request_path = "_debug"
        response = yield http.request('%s%s' % (url, request_path),
            headers={
                'User-Agent': self.iphone_ua,
            })
        self.assertEqual(response.code, 200)
