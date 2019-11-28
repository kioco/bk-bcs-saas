# -*- coding: utf-8 -*-
#
# Tencent is pleased to support the open source community by making 蓝鲸智云PaaS平台社区版 (BlueKing PaaS Community Edition) available.
# Copyright (C) 2017-2019 THL A29 Limited, a Tencent company. All rights reserved.
# Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://opensource.org/licenses/MIT
#
# Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
# an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
# specific language governing permissions and limitations under the License.
#
import logging
import contextlib
from dataclasses import dataclass
import json

import requests
from django.conf import settings
from rest_framework.exceptions import PermissionDenied, APIException
from django.utils.translation import ugettext as _

from . import constants
from backend.components import paas_cc, bcs
from backend.components.utils import http_post
from backend.bcs_k8s import kubectl
from backend.bcs_k8s.utils import get_kubectl_version

logger = logging.getLogger(__name__)


class BCSClusterNotFound(APIException):
    pass


class BCSClusterCredentialsNotFound(APIException):
    pass


@dataclass
class BCSClusterClient:
    """
    用于访问 bcs server 的client简单封装
    功能
    - 注册集群，同时给集群申请 register token
    - 生成 kubectl 连接信息
    """
    host: str  # rest apis are apigw, other apis are settings.BCS_SERVER_HOST
    access_token: str
    project_id: str
    cluster_id: str

    def get_bcs_cluster(self, bcs_api_client):
        bcs_cluster_data = bcs_api_client.query_cluster()
        code_name = bcs_cluster_data.get('code_name')
        if code_name == constants.CLUSTER_NOT_FOUND_CODE_NAME:
            return {}
        if code_name in constants.CLUSTER_PERM_FAIL_CODE_NAMES:
            raise PermissionDenied(
                bcs_cluster_data.get('message', "You do not have permission to perform this action."))
        # 防止后面更新时, 被替换
        bcs_cluster_data['bcs_cluster_id'] = bcs_cluster_data['id']
        return bcs_cluster_data

    def get_cluster(self):
        bcs_api_client = bcs.k8s.K8SClient(self.access_token, self.project_id, self.cluster_id, None)
        # step-1 get bcs cluster id
        bcs_cluster_data = self.get_bcs_cluster(bcs_api_client)
        if not bcs_cluster_data:
            return None

        # step-2 get bcs register token
        register_token_data = bcs_api_client.get_register_tokens(bcs_cluster_data['bcs_cluster_id'])
        if isinstance(register_token_data, list):
            register_token_data = register_token_data[0]
        if register_token_data.get('code_name') == constants.TOKEN_NOT_FOUND_CODE_NAME:
            return None
        # compose the bcs cluster info
        bcs_cluster_data.update(**register_token_data)
        return bcs_cluster_data

    def register_cluster(self):
        bcs_api_client = bcs.k8s.K8SClient(self.access_token, self.project_id, self.cluster_id, None)
        # get bcs cluster data
        bcs_cluster_data = bcs_api_client.register_cluster()
        if bcs_cluster_data.get('code_name') == constants.CLUSTER_EXIST_CODE_NAME:
            bcs_cluster_data = self.get_bcs_cluster(bcs_api_client)
        if bcs_cluster_data.get('code_name') == constants.CLUSTER_NOT_FOUND_CODE_NAME:
            return APIException(_("集群注册失败, bcs server 数据不一致，{}").format(json.dumps(bcs_cluster_data)))

        # 防止被后面被register token返回值覆盖
        bcs_cluster_data['bcs_cluster_id'] = bcs_cluster_data['id']
        # get register token info
        register_token_data = bcs_api_client.create_register_tokens(bcs_cluster_data['id'])
        register_token_data = register_token_data[0]

        bcs_cluster_data.update(**register_token_data)
        return bcs_cluster_data

    def get_or_register_bcs_cluster(self):
        bcs_cluster_data = self.get_cluster()
        if bcs_cluster_data is None:
            bcs_cluster_data = self.register_cluster()
        # TODO: 如果更改为raise，会导致前端也需要变动，先不调整
        return {
            'result': True,
            'message': 'success',
            'data': bcs_cluster_data
        }

    def get_cluster_credential(self):
        bcs_api_client = bcs.k8s.K8SClient(self.access_token, self.project_id, self.cluster_id, None)
        bcs_cluster_data = self.get_cluster()
        if bcs_cluster_data is None:
            raise BCSClusterNotFound("cluster not found, maybe not regist yet.")

        credentials_data = bcs_api_client.get_client_credentials(bcs_cluster_data['bcs_cluster_id'])
        if credentials_data.get('code_name') == constants.CREDENTIALS_NOT_FOUND_CODE_NAME:
            raise BCSClusterCredentialsNotFound(
                _("bcs-agent还没有上报apiserver信息,请检查集群')}[{}]中的kube-system/bcs-agent日志否正常").format(self.cluster_id))
        # 添加 identifier 信息，web-console使用
        credentials_data['identifier'] = bcs_cluster_data['identifier']
        return credentials_data

    def get_access_cluster_context(self):
        """ 获取访问集群需要的信息 """
        context = self.get_cluster_credential()
        context.update(**{
            'host': self.host,
            'source_cluster_id': self.cluster_id,
            'source_project_id': self.project_id,
        })
        return context

    def make_kubectl_options(self):
        context = self.get_access_cluster_context()

        # NOTE: 先兼容两个字段，防止bcs调整发布间隙的问题
        if context.get('server_address_path'):
            server = '{host}{path}'.format(
                host=self.host,
                path=context['server_address_path']
            )
        else:
            server = context['server_address']

        options = {
            'server': server,
            'token': context["user_token"],
            'client-certificate': False,
        }

        # set logs by LOG_LEVEL
        options['v'] = settings.KUBECTL_MAX_VISIBLE_LEVEL

        return options

    @contextlib.contextmanager
    def make_kubectl_client(self):
        options = self.make_kubectl_options()
        cluster = kubectl.Cluster(
            name=self.cluster_id,
            cert=options.pop('client-certificate'),
            server=options.pop('server'),
        )
        user = kubectl.User(
            name=constants.BCS_USER_NAME,
            token=options['token'])
        context = kubectl.Context(
            name=constants.BCS_USER_NAME,
            user=user,
            cluster=cluster
        )
        kubectl_bin_file, version = get_cluster_proper_kubectl(
            self.access_token, self.project_id, self.cluster_id)
        self.k8s_version = version
        kube_config = kubectl.KubeConfig(contexts=[context])
        with kube_config.as_tempfile() as filename:
            kubectl_client = kubectl.KubectlClusterClient(
                kubectl_bin=kubectl_bin_file,
                kubeconfig=filename,
                **options
            )
            yield kubectl_client


def get_cluster_proper_kubectl(access_token, project_id, cluster_id):
    bcs_api_client = bcs.k8s.K8SClient(access_token, project_id, cluster_id, None)

    kubectl_version = get_kubectl_version(
        bcs_api_client.version, constants.KUBECTL_VERSION, constants.DEFAULT_KUBECTL_VERSION)

    try:
        kubectl_bin = settings.KUBECTL_BIN_MAP[kubectl_version]
    except Exception as err:
        logger.error("get kubectl error, kubectl version: %s, error message: %s", kubectl_version, err)
        kubectl_bin = settings.KUBECTL_BIN_MAP[constants.DEFAULT_KUBECTL_VERSION]

    return kubectl_bin, kubectl_version
