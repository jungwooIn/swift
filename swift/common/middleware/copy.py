# Copyright (c) 2015 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Server side copy is a feature that enables users/clients to COPY objects
between accounts and containers without the need to download and then
re-upload objects, thus eliminating additional bandwidth consumption and
also saving time. This may be used when renaming/moving an object which
in Swift is a (COPY + DELETE) operation.

The server side copy middleware should be inserted in the pipeline after auth
and before the quotas and large object middlewares. If it is not present in the
pipeline in the proxy-server configuration file, it will be inserted
automatically. There is no configurable option provided to turn off server
side copy.

--------
Metadata
--------
* All metadata of source object is preserved during object copy.
* One can also provide additional metadata during PUT/COPY request. This will
  over-write any existing conflicting keys.
* Server side copy can also be used to change content-type of an existing
  object.

-----------
Object Copy
-----------
* The destination container must exist before requesting copy of the object.
* When several replicas exist, the system copies from the most recent replica.
  That is, the copy operation behaves as though the X-Newest header is in the
  request.
* The request to copy an object should have no body (i.e. content-length of the
  request must be zero).

There are two ways in which an object can be copied:

1. Send a PUT request to the new object (destination/target) with an additional
   header named ``X-Copy-From`` specifying the source object
   (in '/container/object' format). Example::

    curl -i -X PUT http://<storage_url>/container1/destination_obj
     -H 'X-Auth-Token: <token>'
     -H 'X-Copy-From: /container2/source_obj'
     -H 'Content-Length: 0'

2. Send a COPY request with an existing object in URL with an additional header
   named ``Destination`` specifying the destination/target object
   (in '/container/object' format). Example::

    curl -i -X COPY http://<storage_url>/container2/source_obj
     -H 'X-Auth-Token: <token>'
     -H 'Destination: /container1/destination_obj'
     -H 'Content-Length: 0'

Note that if the incoming request has some conditional headers (e.g. ``Range``,
``If-Match``), the *source* object will be evaluated for these headers (i.e. if
PUT with both ``X-Copy-From`` and ``Range``, Swift will make a partial copy to
the destination object).

-------------------------
Cross Account Object Copy
-------------------------
Objects can also be copied from one account to another account if the user
has the necessary permissions (i.e. permission to read from container
in source account and permission to write to container in destination account).

Similar to examples mentioned above, there are two ways to copy objects across
accounts:

1. Like the example above, send PUT request to copy object but with an
   additional header named ``X-Copy-From-Account`` specifying the source
   account. Example::

    curl -i -X PUT http://<host>:<port>/v1/AUTH_test1/container/destination_obj
     -H 'X-Auth-Token: <token>'
     -H 'X-Copy-From: /container/source_obj'
     -H 'X-Copy-From-Account: AUTH_test2'
     -H 'Content-Length: 0'

2. Like the previous example, send a COPY request but with an additional header
   named ``Destination-Account`` specifying the name of destination account.
   Example::

    curl -i -X COPY http://<host>:<port>/v1/AUTH_test2/container/source_obj
     -H 'X-Auth-Token: <token>'
     -H 'Destination: /container/destination_obj'
     -H 'Destination-Account: AUTH_test1'
     -H 'Content-Length: 0'

-------------------
Large Object Copy
-------------------
The best option to copy a large option is to copy segments individually.
To copy the manifest object of a large object, add the query parameter to
the copy request::

    ?multipart-manifest=get

If a request is sent without the query parameter, an attempt will be made to
copy the whole object but will fail if the object size is
greater than 5GB.

-------------------
Object Post as Copy
-------------------
Historically, this has been a feature (and a configurable option with default
set to True) in proxy server configuration. This has been moved to server side
copy middleware.

When ``object_post_as_copy`` is set to ``true`` (default value), an incoming
POST request is morphed into a COPY request where source and destination
objects are same.

This feature was necessary because of a previous behavior where POSTS would
update the metadata on the object but not on the container. As a result,
features like container sync would not work correctly. This is no longer the
case and the plan is to deprecate this option. It is being kept now for
backwards compatibility. At first chance, set ``object_post_as_copy`` to
``false``.
"""

import os
from urllib import quote
from ConfigParser import ConfigParser, NoSectionError, NoOptionError
from six.moves.urllib.parse import unquote

from swift.common import utils
from swift.common.utils import get_logger, \
    config_true_value, FileLikeIter, read_conf_dir, close_if_possible
from swift.common.swob import Request, HTTPPreconditionFailed, \
    HTTPRequestEntityTooLarge, HTTPBadRequest
from swift.common.http import HTTP_MULTIPLE_CHOICES, HTTP_CREATED, \
    is_success
from swift.common.constraints import check_account_format, MAX_FILE_SIZE
from swift.common.request_helpers import copy_header_subset, remove_items, \
    is_sys_meta, is_sys_or_user_meta, is_object_transient_sysmeta
from swift.common.wsgi import WSGIContext, make_subrequest


def _check_path_header(req, name, length, error_msg):
    """
    Validate that the value of path-like header is
    well formatted. We assume the caller ensures that
    specific header is present in req.headers.

    :param req: HTTP request object
    :param name: header name
    :param length: length of path segment check
    :param error_msg: error message for client
    :returns: A tuple with path parts according to length
    :raise: HTTPPreconditionFailed if header value
            is not well formatted.
    """
    src_header = unquote(req.headers.get(name))
    if not src_header.startswith('/'):
        src_header = '/' + src_header
    try:
        return utils.split_path(src_header, length, length, True)
    except ValueError:
        raise HTTPPreconditionFailed(
            request=req,
            body=error_msg)


def _check_copy_from_header(req):
    """
    Validate that the value from x-copy-from header is
    well formatted. We assume the caller ensures that
    x-copy-from header is present in req.headers.

    :param req: HTTP request object
    :returns: A tuple with container name and object name
    :raise: HTTPPreconditionFailed if x-copy-from value
            is not well formatted.
    """
    return _check_path_header(req, 'X-Copy-From', 2,
                              'X-Copy-From header must be of the form '
                              '<container name>/<object name>')


def _check_destination_header(req):
    """
    Validate that the value from destination header is
    well formatted. We assume the caller ensures that
    destination header is present in req.headers.

    :param req: HTTP request object
    :returns: A tuple with container name and object name
    :raise: HTTPPreconditionFailed if destination value
            is not well formatted.
    """
    return _check_path_header(req, 'Destination', 2,
                              'Destination header must be of the form '
                              '<container name>/<object name>')


def _copy_headers_into(from_r, to_r):
    """
    Will copy desired headers from from_r to to_r
    :params from_r: a swob Request or Response
    :params to_r: a swob Request or Response
    """
    pass_headers = ['x-delete-at']
    for k, v in from_r.headers.items():
        if (is_sys_or_user_meta('object', k) or
                is_object_transient_sysmeta(k) or
                k.lower() in pass_headers):
            to_r.headers[k] = v


class ServerSideCopyWebContext(WSGIContext):

    def __init__(self, app, logger):
        super(ServerSideCopyWebContext, self).__init__(app)
        self.app = app
        self.logger = logger

    def get_source_resp(self, req):
        sub_req = make_subrequest(
            req.environ, path=req.path_info, headers=req.headers,
            swift_source='SSC')
        return sub_req.get_response(self.app)

    def send_put_req(self, req, additional_resp_headers, start_response):
        app_resp = self._app_call(req.environ)
        self._adjust_put_response(req, additional_resp_headers)
        start_response(self._response_status,
                       self._response_headers,
                       self._response_exc_info)
        return app_resp

    def _adjust_put_response(self, req, additional_resp_headers):
        if 'swift.post_as_copy' in req.environ:
            # Older editions returned 202 Accepted on object POSTs, so we'll
            # convert any 201 Created responses to that for compatibility with
            # picky clients.
            if self._get_status_int() == HTTP_CREATED:
                self._response_status = '202 Accepted'
        elif is_success(self._get_status_int()):
            for header, value in additional_resp_headers.items():
                self._response_headers.append((header, value))

    def handle_OPTIONS_request(self, req, start_response):
        app_resp = self._app_call(req.environ)
        if is_success(self._get_status_int()):
            for i, (header, value) in enumerate(self._response_headers):
                if header.lower() == 'allow' and 'COPY' not in value:
                    self._response_headers[i] = ('Allow', value + ', COPY')
                if header.lower() == 'access-control-allow-methods' and \
                        'COPY' not in value:
                    self._response_headers[i] = \
                        ('Access-Control-Allow-Methods', value + ', COPY')
        start_response(self._response_status,
                       self._response_headers,
                       self._response_exc_info)
        return app_resp


class ServerSideCopyMiddleware(object):

    def __init__(self, app, conf):
        self.app = app
        self.logger = get_logger(conf, log_route="copy")
        # Read the old object_post_as_copy option from Proxy app just in case
        # someone has set it to false (non default). This wouldn't cause
        # problems during upgrade.
        self._load_object_post_as_copy_conf(conf)
        self.object_post_as_copy = \
            config_true_value(conf.get('object_post_as_copy', 'true'))

    def _load_object_post_as_copy_conf(self, conf):
        if ('object_post_as_copy' in conf or '__file__' not in conf):
            # Option is explicitly set in middleware conf. In that case,
            # we assume operator knows what he's doing.
            # This takes preference over the one set in proxy app
            return

        cp = ConfigParser()
        if os.path.isdir(conf['__file__']):
            read_conf_dir(cp, conf['__file__'])
        else:
            cp.read(conf['__file__'])

        try:
            pipe = cp.get("pipeline:main", "pipeline")
        except (NoSectionError, NoOptionError):
            return

        proxy_name = pipe.rsplit(None, 1)[-1]
        proxy_section = "app:" + proxy_name

        try:
            conf['object_post_as_copy'] = cp.get(proxy_section,
                                                 'object_post_as_copy')
        except (NoSectionError, NoOptionError):
            pass

    def __call__(self, env, start_response):
        req = Request(env)
        try:
            (version, account, container, obj) = req.split_path(4, 4, True)
        except ValueError:
            # If obj component is not present in req, do not proceed further.
            return self.app(env, start_response)

        self.account_name = account
        self.container_name = container
        self.object_name = obj

        # Save off original request method (COPY/POST) in case it gets mutated
        # into PUT during handling. This way logging can display the method
        # the client actually sent.
        req.environ['swift.orig_req_method'] = req.method

        if req.method == 'PUT' and req.headers.get('X-Copy-From'):
            return self.handle_PUT(req, start_response)
        elif req.method == 'COPY':
            return self.handle_COPY(req, start_response)
        elif req.method == 'POST' and self.object_post_as_copy:
            return self.handle_object_post_as_copy(req, start_response)
        elif req.method == 'OPTIONS':
            # Does not interfere with OPTIONS response from (account,container)
            # servers and /info response.
            return self.handle_OPTIONS(req, start_response)

        return self.app(env, start_response)

    def handle_object_post_as_copy(self, req, start_response):
        req.method = 'PUT'
        req.path_info = '/v1/%s/%s/%s' % (
            self.account_name, self.container_name, self.object_name)
        req.headers['Content-Length'] = 0
        req.headers.pop('Range', None)
        req.headers['X-Copy-From'] = quote('/%s/%s' % (self.container_name,
                                           self.object_name))
        req.environ['swift.post_as_copy'] = True
        params = req.params
        # for post-as-copy always copy the manifest itself if source is *LO
        params['multipart-manifest'] = 'get'
        req.params = params
        return self.handle_PUT(req, start_response)

    def handle_COPY(self, req, start_response):
        if not req.headers.get('Destination'):
            return HTTPPreconditionFailed(request=req,
                                          body='Destination header required'
                                          )(req.environ, start_response)
        dest_account = self.account_name
        if 'Destination-Account' in req.headers:
            dest_account = req.headers.get('Destination-Account')
            dest_account = check_account_format(req, dest_account)
            req.headers['X-Copy-From-Account'] = self.account_name
            self.account_name = dest_account
            del req.headers['Destination-Account']
        dest_container, dest_object = _check_destination_header(req)
        source = '/%s/%s' % (self.container_name, self.object_name)
        self.container_name = dest_container
        self.object_name = dest_object
        # re-write the existing request as a PUT instead of creating a new one
        req.method = 'PUT'
        # As this the path info is updated with destination container,
        # the proxy server app will use the right object controller
        # implementation corresponding to the container's policy type.
        ver, _junk = req.split_path(1, 2, rest_with_last=True)
        req.path_info = '/%s/%s/%s/%s' % \
                        (ver, dest_account, dest_container, dest_object)
        req.headers['Content-Length'] = 0
        req.headers['X-Copy-From'] = quote(source)
        del req.headers['Destination']
        return self.handle_PUT(req, start_response)

    def _get_source_object(self, ssc_ctx, source_path, req):
        source_req = req.copy_get()

        # make sure the source request uses it's container_info
        source_req.headers.pop('X-Backend-Storage-Policy-Index', None)
        source_req.path_info = quote(source_path)
        source_req.headers['X-Newest'] = 'true'
        if 'swift.post_as_copy' in req.environ:
            # We're COPYing one object over itself because of a POST; rely on
            # the PUT for write authorization, don't require read authorization
            source_req.environ['swift.authorize'] = lambda req: None
            source_req.environ['swift.authorize_override'] = True

        # in case we are copying an SLO manifest, set format=raw parameter
        params = source_req.params
        if params.get('multipart-manifest') == 'get':
            params['format'] = 'raw'
            source_req.params = params

        source_resp = ssc_ctx.get_source_resp(source_req)

        if source_resp.content_length is None:
            # This indicates a transfer-encoding: chunked source object,
            # which currently only happens because there are more than
            # CONTAINER_LISTING_LIMIT segments in a segmented object. In
            # this case, we're going to refuse to do the server-side copy.
            return HTTPRequestEntityTooLarge(request=req)

        if source_resp.content_length > MAX_FILE_SIZE:
            return HTTPRequestEntityTooLarge(request=req)

        return source_resp

    def _create_response_headers(self, source_path, source_resp, sink_req):
        resp_headers = dict()
        acct, path = source_path.split('/', 3)[2:4]
        resp_headers['X-Copied-From-Account'] = quote(acct)
        resp_headers['X-Copied-From'] = quote(path)
        if 'last-modified' in source_resp.headers:
                resp_headers['X-Copied-From-Last-Modified'] = \
                    source_resp.headers['last-modified']
        # Existing sys and user meta of source object is added to response
        # headers in addition to the new ones.
        for k, v in sink_req.headers.items():
            if is_sys_or_user_meta('object', k) or k.lower() == 'x-delete-at':
                resp_headers[k] = v
        return resp_headers

    def handle_PUT(self, req, start_response):
        if req.content_length:
            return HTTPBadRequest(body='Copy requests require a zero byte '
                                  'body', request=req,
                                  content_type='text/plain')(req.environ,
                                                             start_response)

        # Form the path of source object to be fetched
        ver, acct, _rest = req.split_path(2, 3, True)
        src_account_name = req.headers.get('X-Copy-From-Account')
        if src_account_name:
            src_account_name = check_account_format(req, src_account_name)
        else:
            src_account_name = acct
        src_container_name, src_obj_name = _check_copy_from_header(req)
        source_path = '/%s/%s/%s/%s' % (ver, src_account_name,
                                        src_container_name, src_obj_name)

        if req.environ.get('swift.orig_req_method', req.method) != 'POST':
            self.logger.info("Copying object from %s to %s" %
                             (source_path, req.path))

        # GET the source object, bail out on error
        ssc_ctx = ServerSideCopyWebContext(self.app, self.logger)
        source_resp = self._get_source_object(ssc_ctx, source_path, req)
        if source_resp.status_int >= HTTP_MULTIPLE_CHOICES:
            close_if_possible(source_resp.app_iter)
            return source_resp(source_resp.environ, start_response)

        # Create a new Request object based on the original req instance.
        # This will preserve env and headers.
        sink_req = Request.blank(req.path_info,
                                 environ=req.environ, headers=req.headers)

        params = sink_req.params
        if params.get('multipart-manifest') == 'get':
            if 'X-Static-Large-Object' in source_resp.headers:
                params['multipart-manifest'] = 'put'
            if 'X-Object-Manifest' in source_resp.headers:
                del params['multipart-manifest']
                sink_req.headers['X-Object-Manifest'] = \
                    source_resp.headers['X-Object-Manifest']
            sink_req.params = params

        # Set data source, content length and etag for the PUT request
        sink_req.environ['wsgi.input'] = FileLikeIter(source_resp.app_iter)
        sink_req.content_length = source_resp.content_length
        sink_req.etag = source_resp.etag

        # We no longer need these headers
        sink_req.headers.pop('X-Copy-From', None)
        sink_req.headers.pop('X-Copy-From-Account', None)
        # If the copy request does not explicitly override content-type,
        # use the one present in the source object.
        if not req.headers.get('content-type'):
            sink_req.headers['Content-Type'] = \
                source_resp.headers['Content-Type']

        fresh_meta_flag = config_true_value(
            sink_req.headers.get('x-fresh-metadata', 'false'))

        if fresh_meta_flag or 'swift.post_as_copy' in sink_req.environ:
            # Post-as-copy: ignore new sysmeta, copy existing sysmeta
            condition = lambda k: is_sys_meta('object', k)
            remove_items(sink_req.headers, condition)
            copy_header_subset(source_resp, sink_req, condition)
        else:
            # Copy/update existing sysmeta, transient-sysmeta and user meta
            _copy_headers_into(source_resp, sink_req)
            # Copy/update new metadata provided in request if any
            _copy_headers_into(req, sink_req)

        # Create response headers for PUT response
        resp_headers = self._create_response_headers(source_path,
                                                     source_resp, sink_req)

        put_resp = ssc_ctx.send_put_req(sink_req, resp_headers, start_response)
        close_if_possible(source_resp.app_iter)
        return put_resp

    def handle_OPTIONS(self, req, start_response):
        return ServerSideCopyWebContext(self.app, self.logger).\
            handle_OPTIONS_request(req, start_response)


def filter_factory(global_conf, **local_conf):
    conf = global_conf.copy()
    conf.update(local_conf)

    def copy_filter(app):
        return ServerSideCopyMiddleware(app, conf)

    return copy_filter
