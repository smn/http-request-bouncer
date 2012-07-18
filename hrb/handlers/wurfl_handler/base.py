import hashlib
import json

from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.internet import protocol, reactor
from twisted.protocols.memcache import MemCacheProtocol, DEFAULT_PORT

from pywurfl.algorithms import TwoStepAnalysis

from hrb.handlers.base import BaseHandler
from hrb.handlers.wurfl_handler import wurfl_devices


class WurflHandler(BaseHandler):
    def validate_config(self, config):
        self.cookie_name = config.get('cookie_name', 'X-UA-map')
        self.cache_prefix = config.get('cache_prefix', '')
        self.cache_prefix_delimiter = config.get('cache_prefix_delimiter', '#')
        self.cache_lifetime = int(config.get('cache_lifetime', 0))
        self.memcached_config = config.get('memcached', {})

    @inlineCallbacks
    def setup_handler(self):
        self.devices = wurfl_devices.devices
        self.algorithm = TwoStepAnalysis(self.devices)
        self.memcached = yield self.connect_to_memcached(
                **self.memcached_config)
        self.namespace = yield self.get_namespace()
        returnValue(self)

    @inlineCallbacks
    def connect_to_memcached(self, host="localhost", port=DEFAULT_PORT):
        creator = protocol.ClientCreator(reactor, MemCacheProtocol)
        client = yield creator.connectTCP(host, port)
        returnValue(client)

    def get_namespace_key(self):
        return '%s_namespace' % (self.cache_prefix,)

    @inlineCallbacks
    def get_namespace_version(self):
        namespace_key = self.get_namespace_key()
        _, version = yield self.memcached.get(namespace_key)
        if not version:
            version = 0
            yield self.memcached.set(namespace_key, version)
        returnValue(str(version))

    @inlineCallbacks
    def get_namespace(self):
        version = yield self.get_namespace_version()
        returnValue(''.join([
            self.cache_prefix,
            self.cache_prefix_delimiter,
            version,
        ]))

    @inlineCallbacks
    def increment_namespace(self, value=1):
        namespace_key = self.get_namespace_key()
        yield self.memcached.increment(namespace_key, value)
        self.namespace = self.get_namespace()

    @inlineCallbacks
    def handle_request(self, request):
        user_agent = unicode(request.getHeader('User-Agent') or '')
        cache_key = self.get_cache_key(user_agent)
        flags, cached = yield self.memcached.get(cache_key)
        if not cached:
            body = yield self.handle_request_and_cache(cache_key,
                user_agent, request)
        else:
            body = self.handle_request_from_cache(cached, request)
        returnValue(body)

    @inlineCallbacks
    def handle_request_and_cache(self, cache_key, user_agent, request):
        device = self.devices.select_ua(user_agent, search=self.algorithm)

        # Make copies
        original_headers = request.responseHeaders.copy()
        original_cookies = request.cookies[:]
        body = self.handle_device(request, device)
        # Make new copies for comparison
        new_headers = request.responseHeaders.copy()
        new_cookies = request.cookies[:]

        # Compare & leave what's new
        for header, _ in original_headers.getAllRawHeaders():
            if new_headers.hasHeader(header):
                new_headers.removeHeader(header)

        for cookie in original_cookies:
            if cookie in new_cookies:
                new_cookies.remove(cookie)

        yield self.memcached.set(cache_key, json.dumps({
            'headers': new_headers._rawHeaders,
            'cookies': new_cookies,
            'body': body,
        }), expireTime=self.cache_lifetime)

        returnValue(body)

    def handle_request_from_cache(self, cached, request):
        # JSON returns everything as unicode which Twisted isn't too happy
        # with, encode to utf8 bytestring instead.
        data = json.loads(cached)
        for key, value in data['headers'].items():
            request.setHeader(key.encode('utf8'), value.encode('utf8'))
        request.cookies.extend([c.encode('utf8') for c in data['cookies']])
        return (data.get('body') or '').encode('utf8')

    def get_cache_key(self, key):
        return ''.join([
            self.namespace,
            self.cache_prefix_delimiter,
            hashlib.md5(key).hexdigest()
        ])

    def handle_device(self, request, device):
        raise NotImplementedError("Subclasses should implement this")
