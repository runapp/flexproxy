#!/usr/bin/env python

import logging
import os
import sys
import socket
import asyncio

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import tornado.httpserver
import tornado.ioloop
import tornado.iostream
import tornado.web
import tornado.httpclient
import tornado.httputil
from tornado.options import define, options

define('port', default=8875, help='Port the proxy server runs on')
define('bind', default='127.0.0.1', help='Address the proxy server binds to')
define('up_host', default=None, type=str, help='Upstream proxy host')
define('up_port', default=0, help='Upstream proxy port')

logger = logging.getLogger('tornado_proxy')

__all__ = ['ProxyHandler', 'run_proxy']


def fetch_request(url, up_proxy, **kwargs):
    if up_proxy:
        logger.debug('Forward request via upstream proxy %s:%d', *up_proxy)
        tornado.httpclient.AsyncHTTPClient.configure(
            'tornado.curl_httpclient.CurlAsyncHTTPClient')
        kwargs['proxy_host'] = up_proxy[0]
        kwargs['proxy_port'] = up_proxy[1]

    req = tornado.httpclient.HTTPRequest(url, **kwargs)
    client = tornado.httpclient.AsyncHTTPClient()
    return client.fetch(req, raise_error=False)


async def pipe(f: tornado.iostream.IOStream, t: tornado.iostream.IOStream):
    try:
        while True:
            a = await f.read_bytes(16384, partial=True)
            await t.write(a)
    except tornado.iostream.StreamClosedError as e:
        pass


class ProxyHandler(tornado.web.RequestHandler):
    SUPPORTED_METHODS = ['GET', 'POST', 'CONNECT']
    proxy = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if options.up_host and options.up_port:
            self.proxy = (options.up_host, options.up_port)
            logger.info("Upstream proxy set to %s:%d", *self.proxy)

    def compute_etag(self):
        return None  # disable tornado Etag

    async def get(self):
        logger.debug('Handle %s request to %s', self.request.method, self.request.uri)

        body = self.request.body
        if not body:
            body = None
        try:
            if 'Proxy-Connection' in self.request.headers:
                del self.request.headers['Proxy-Connection']
            response = await fetch_request(
                self.request.uri,
                self.proxy,
                method=self.request.method, body=body,
                headers=self.request.headers, follow_redirects=False,
                allow_nonstandard_methods=True)
        except tornado.httpclient.HTTPError as e:
            if hasattr(e, 'response') and e.response:
                pass
            else:
                self.set_status(500)
                self.write('Internal server error:\n' + str(e))
                return
        except ConnectionRefusedError as e:
            self.set_status(502)
            self.write('Remote connection refused.')
            return

        if (response.error and not
                isinstance(response.error, tornado.httpclient.HTTPError)):
            self.set_status(500)
            self.write('Internal server error:\n' + str(response.error))
        else:
            self.set_status(response.code, response.reason)
            self._headers = tornado.httputil.HTTPHeaders()  # clear tornado default header

            for header, v in response.headers.get_all():
                if header not in ('Content-Length', 'Transfer-Encoding', 'Content-Encoding', 'Connection'):
                    self.add_header(header, v)  # some header appear multiple times, eg 'Set-Cookie'

            if response.body:
                self.set_header('Content-Length', len(response.body))
                self.write(response.body)

    async def post(self):
        return self.get()

    async def connect(self):
        logger.debug('Start CONNECT to %s', self.request.uri)
        host, port = self.request.uri.split(':')  # Here the uri only contains "host:port" for CONNECT requests.
        client: tornado.iostream.IOStream = self.request.connection.stream

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
        upstream = tornado.iostream.IOStream(s)

        if self.proxy:
            await upstream.connect(self.proxy)
            # It's safe to encode as ascii, as non-ascii domains are already encoded.
            await upstream.write(b'CONNECT %s HTTP/1.1\r\n' % self.request.uri.encode('ascii'))
            await upstream.write(b'Host: %s\r\n' % host.encode('ascii'))
            await upstream.write(b'Proxy-Connection: Keep-Alive\r\n\r\n')
            data = await upstream.read_until(b'\r\n\r\n')
            if data:
                first_line = data.splitlines()[0]
                _, status, _ = first_line.split(None, 2)
                status = int(status)
            else:
                status = -1
            if status != 200:
                self.set_status(status)
                return

            logger.debug('Connected to upstream proxy %s:%d', *self.proxy)
        else:
            await upstream.connect((host, int(port)))

        logger.debug('CONNECT tunnel established to %s', self.request.uri)
        try:
            await client.write(b'HTTP/1.0 200 Connection established\r\n\r\n')
        except tornado.iostream.StreamClosedError as e:
            return
        await asyncio.gather(pipe(upstream, client), pipe(client, upstream))


def run_proxy(port, address, start_ioloop=True):
    """
    Run proxy on the specified port. If start_ioloop is True (default),
    the tornado IOLoop will be started immediately.
    """
    app = tornado.web.Application([
        (r'.*', ProxyHandler),
    ])
    app.listen(port, address)
    ioloop = tornado.ioloop.IOLoop.instance()
    if start_ioloop:
        ioloop.start()


if __name__ == '__main__':
    options.parse_command_line()
    run_proxy(options.port, options.bind)
