# Tweepy
# Copyright 2009-2010 Joshua Roesslein
# See LICENSE for details.

# External modules
import httplib
import urllib
import time
import re
from StringIO import StringIO
import gzip
import tornado.httpclient
import tornado.httputil
from tornado import gen

# Internal modules
from tweepy.error import TweepError
from tweepy.utils import convert_to_utf8_str
from tweepy.models import Model

re_path_template = re.compile('{\w+}')


def bind_api(**config):

    class APIMethod(object):

        path = config['path']
        payload_type = config.get('payload_type', None)
        payload_list = config.get('payload_list', False)
        allowed_param = config.get('allowed_param', [])
        method = config.get('method', 'GET')
        require_auth = config.get('require_auth', False)
        search_api = config.get('search_api', False)
        use_cache = config.get('use_cache', True)

        def __init__(self, api, args, kargs):
            # If authentication is required and no credentials
            # are provided, throw an error.
            if self.require_auth and not api.auth:
                raise TweepError('Authentication required!')

            self.api = api
            self.post_data = kargs.pop('post_data', None)
            self.retry_count = kargs.pop('retry_count', api.retry_count)
            self.retry_delay = kargs.pop('retry_delay', api.retry_delay)
            self.retry_errors = kargs.pop('retry_errors', api.retry_errors)
            self.headers = kargs.pop('headers', {})
            self.build_parameters(args, kargs)

            # Pick correct URL root to use
            if self.search_api:
                self.api_root = api.search_root
            else:
                self.api_root = api.api_root

            # Perform any path variable substitution
            self.build_path()

            if api.secure:
                self.scheme = 'https://'
            else:
                self.scheme = 'http://'

            if self.search_api:
                self.host = api.search_host
            else:
                self.host = api.host

            # Manually set Host header to fix an issue in python 2.5
            # or older where Host is set including the 443 port.
            # This causes Twitter to issue 301 redirect.
            # See Issue https://github.com/tweepy/tweepy/issues/12
            self.headers['Host'] = self.host

        def build_parameters(self, args, kargs):
            self.parameters = {}
            for idx, arg in enumerate(args):
                if arg is None:
                    continue

                try:
                    self.parameters[self.allowed_param[idx]] = convert_to_utf8_str(arg)
                except IndexError:
                    raise TweepError('Too many parameters supplied!')

            for k, arg in kargs.items():
                if arg is None:
                    continue
                if k in self.parameters:
                    raise TweepError('Multiple values for parameter %s supplied!' % k)

                self.parameters[k] = convert_to_utf8_str(arg)

        def build_path(self):
            for variable in re_path_template.findall(self.path):
                name = variable.strip('{}')

                if name == 'user' and 'user' not in self.parameters and self.api.auth:
                    # No 'user' parameter provided, fetch it from Auth instead.
                    value = self.api.auth.get_username()
                else:
                    try:
                        value = urllib.quote(self.parameters[name])
                    except KeyError:
                        raise TweepError('No parameter value found for path variable: %s' % name)
                    del self.parameters[name]

                self.path = self.path.replace(variable, value)
        
        def get_cache(self, url):
            # Query the cache if one is available
            # and this request uses a GET method.
            if self.api.cache and self.method == 'GET':
                cache_result = self.api.cache.get(url)
                # if cache result found and not expired, return it
                if cache_result:
                    # must restore api reference
                    if isinstance(cache_result, list):
                        for result in cache_result:
                            if isinstance(result, Model):
                                result._api = self.api
                    else:
                        if isinstance(cache_result, Model):
                            cache_result._api = self.api
                    return cache_result
            return None

        @gen.coroutine
        def execute_async(self):
            # Build the request URL
            url = self.api_root + self.path
            if len(self.parameters):
                url = '%s?%s' % (url, urllib.urlencode(self.parameters))

            cache = self.get_cache(url)
            if cache:
                raise gen.Return(cache)

            # Apply authentication
            if self.api.auth:
                self.api.auth.apply_auth(
                    self.scheme + self.host + url,
                    self.method, self.headers, self.parameters
                    )
            url = self.scheme + self.host + url
            req = tornado.httpclient.HTTPRequest(url=url, headers=self.headers, method='GET')
            conn = tornado.httpclient.AsyncHTTPClient()

            try:
                response = yield conn.fetch(req)
            except tornado.httpclient.HTTPError as err:
                try:
                    error_msg = self.api.parser.parse_error(err.response.body)
                except Exception:
                    error_msg = "Twitter error response: status code = %s" % err.code
                raise TweepError(error_msg, response)
            # Parse the response payload
            raise gen.Return(self.api.parser.parse(self, response.body))

    @gen.coroutine
    def _call(api, *args, **kargs):
        method = APIMethod(api, args, kargs)
        result = yield method.execute_async()
        raise gen.Return(result)

    # Set pagination mode
    if 'cursor' in APIMethod.allowed_param:
        _call.pagination_mode = 'cursor'
    elif 'max_id' in APIMethod.allowed_param and \
         'since_id' in APIMethod.allowed_param:
        _call.pagination_mode = 'id'
    elif 'page' in APIMethod.allowed_param:
        _call.pagination_mode = 'page'

    return _call

