# coding=utf-8
# Copyright (c) 2011 OpenStack Foundation
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
"""
Common Auth Middleware.

"""

from oslo_config import cfg
from oslo_log import log as logging
from oslo_middleware import request_id
from oslo_serialization import jsonutils
import webob.dec
import webob.exc

from nova import context
from nova.i18n import _
from nova import wsgi


auth_opts = [
    cfg.BoolOpt('api_rate_limit',
                default=False,
                help='Whether to use per-user rate limiting for the api. '
                     'This option is only used by v2 api. Rate limiting '
                     'is removed from v2.1 api.'),
    cfg.StrOpt('auth_strategy',
               default='keystone',
               help='''
The strategy to use for auth: keystone or noauth2. noauth2 is designed for
testing only, as it does no actual credential checking. noauth2 provides
administrative credentials only if 'admin' is specified as the username.
'''),
    cfg.BoolOpt('use_forwarded_for',
                default=False,
                help='Treat X-Forwarded-For as the canonical remote address. '
                     'Only enable this if you have a sanitizing proxy.'),
]

CONF = cfg.CONF
CONF.register_opts(auth_opts)

LOG = logging.getLogger(__name__)


def _load_pipeline(loader, pipeline):
    ### 当配置文件中auth_strategy='keystone'时：
    ### loader: <paste.deploy.loadwsgi.ConfigLoader object at 0x5af7390>
    ### pipeline: ['compute_req_id', 'faultwrap', 'sizelimit', 'authtoken', 'keystonecontext', 'legacy_v2_compatible', 'osapi_compute_app_v21']

    ### filters: [
    ###     <class 'nova.api.compute_req_id.ComputeReqIdMiddleware'>,
    ###     <function _factory at 0x5b07b90>,
    ###     <function middleware_filter at 0x5b07c80>,
    ###     <function auth_filter at 0x5dc1c08>,
    ###     <function _factory at 0x5dc1c80>,
    ###     <function _factory at 0x5dc1cf8>
    ### ]
    filters = [loader.get_filter(n) for n in pipeline[:-1]]

    ### app: <nova.api.openstack.compute.APIRouterV21 object at 0x7c8bf10>
    app = loader.get_app(pipeline[-1])

    ### 将列表filters中的元素反向
    filters.reverse()
    for filter in filters:
        app = filter(app)

    ### app: <nova.api.compute_req_id.ComputeReqIdMiddleware object at 0x7e9aad0>
    return app


def pipeline_factory(loader, global_conf, **local_conf):
    """A paste pipeline replica that keys off of auth_strategy."""

    pipeline = local_conf[CONF.auth_strategy]
    if not CONF.api_rate_limit:
        limit_name = CONF.auth_strategy + '_nolimit'
        pipeline = local_conf.get(limit_name, pipeline)
    pipeline = pipeline.split()
    return _load_pipeline(loader, pipeline)


def pipeline_factory_v21(loader, global_conf, **local_conf):
    """A paste pipeline replica that keys off of auth_strategy."""
    ### loader: <paste.deploy.loadwsgi.ConfigLoader object at 0x5af7390>
    ### global_conf: {'__file__': '/etc/nova/api-paste.ini', 'here': '/etc/nova'}
    ### local_conf: {
    ###     'noauth2': 'compute_req_id faultwrap sizelimit noauth2 legacy_v2_compatible osapi_compute_app_v21',
    ###     'keystone': 'compute_req_id faultwrap sizelimit authtoken keystonecontext legacy_v2_compatible osapi_compute_app_v21'
    ### }
    return _load_pipeline(loader, local_conf[CONF.auth_strategy].split())


# NOTE(oomichi): This pipeline_factory_v3 is for passing check-grenade-dsvm.
pipeline_factory_v3 = pipeline_factory_v21


class InjectContext(wsgi.Middleware):
    """Add a 'nova.context' to WSGI environ."""

    def __init__(self, context, *args, **kwargs):
        self.context = context
        super(InjectContext, self).__init__(*args, **kwargs)

    @webob.dec.wsgify(RequestClass=wsgi.Request)
    def __call__(self, req):
        req.environ['nova.context'] = self.context
        return self.application


class NovaKeystoneContext(wsgi.Middleware):
    """Make a request context from keystone headers."""

    ### class Request(webob.Request):
    ###     def __init__(self, environ, *args, **kwargs):
    ###         if CONF.secure_proxy_ssl_header:
    ###             scheme = environ.get(CONF.secure_proxy_ssl_header)
    ###         if scheme:
    ###            environ['wsgi.url_scheme'] = scheme
    ###    super(Request, self).__init__(environ, *args, **kwargs)

    @webob.dec.wsgify(RequestClass=wsgi.Request)
    def __call__(self, req):
        user_id = req.headers.get('X_USER')
        user_id = req.headers.get('X_USER_ID', user_id)
        if user_id is None:
            LOG.debug("Neither X_USER_ID nor X_USER found in request")
            return webob.exc.HTTPUnauthorized()

        roles = self._get_roles(req)

        if 'X_TENANT_ID' in req.headers:
            # This is the new header since Keystone went to ID/Name
            project_id = req.headers['X_TENANT_ID']
        else:
            # This is for legacy compatibility
            project_id = req.headers['X_TENANT']
        project_name = req.headers.get('X_TENANT_NAME')
        user_name = req.headers.get('X_USER_NAME')

        req_id = req.environ.get(request_id.ENV_REQUEST_ID)

        # Get the auth token
        auth_token = req.headers.get('X_AUTH_TOKEN',
                                     req.headers.get('X_STORAGE_TOKEN'))

        # Build a context, including the auth_token...
        remote_address = req.remote_addr
        if CONF.use_forwarded_for:
            remote_address = req.headers.get('X-Forwarded-For', remote_address)

        service_catalog = None
        if req.headers.get('X_SERVICE_CATALOG') is not None:
            try:
                catalog_header = req.headers.get('X_SERVICE_CATALOG')
                service_catalog = jsonutils.loads(catalog_header)
            except ValueError:
                raise webob.exc.HTTPInternalServerError(
                          _('Invalid service catalog json.'))

        # NOTE(jamielennox): This is a full auth plugin set by auth_token
        # middleware in newer versions.
        user_auth_plugin = req.environ.get('keystone.token_auth')

        ctx = context.RequestContext(user_id,
                                     project_id,
                                     user_name=user_name,
                                     project_name=project_name,
                                     roles=roles,
                                     auth_token=auth_token,
                                     remote_address=remote_address,
                                     service_catalog=service_catalog,
                                     request_id=req_id,
                                     user_auth_plugin=user_auth_plugin)

        req.environ['nova.context'] = ctx
        return self.application

    def _get_roles(self, req):
        """Get the list of roles."""

        roles = req.headers.get('X_ROLES', '')
        return [r.strip() for r in roles.split(',')]
