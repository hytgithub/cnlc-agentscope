"""
测井预测大模型智能体工具集
包含：NL2SQL 工具、数据预处理工具、测井预测大模型推理工具、可视化工具
"""
import json
import re
import math
import os
from typing import Dict, Any
from collections import Counter
from typing import List, Optional

import requests

from cnlc_agent.wplm.agentscope_tool import AgentScopeJsonTool

# 使用曲线数据接口请导入
from cnlc_agent.pygdsx.curve import *
from cnlc_agent.pygdsx.welllog import create_gdx_log
from cnlc_agent.pygdsx.table import BDTableInfo, create_gdsx_table, write_gdx_table_all_data

# DataPreProcess 服务地址
DPP_BASE_URL = os.environ.get('DPP_BASE_URL', 'http://localhost:8686')
DPP_TIMEOUT_SECONDS = int(os.environ.get('DPP_TIMEOUT_SECONDS', '300'))
_LOGIN_URL = os.environ.get('WELLLOG_LOGIN_URL', '')
_API_USERNAME = os.environ.get('WELLLOG_API_USERNAME', '')
_API_PASSWORD = os.environ.get('WELLLOG_API_PASSWORD', '')


def _welllog_login() -> str:
    """调用测井平台登录接口获取 token（POST + query params，token 作为裸 Authorization 传递）"""
    if not all((_LOGIN_URL, _API_USERNAME, _API_PASSWORD)):
        raise RuntimeError('测井平台登录配置不完整')
    resp = requests.post(_LOGIN_URL, params={'username': _API_USERNAME, 'password': _API_PASSWORD}, timeout=30)
    resp.raise_for_status()
    data = json.loads(resp.text)
    if data.get('code') != 200 or not data.get('data', {}).get('token'):
        raise RuntimeError('测井平台登录失败')
    return data['data']['token']

class DataPreprocessingTool(AgentScopeJsonTool):
    """测井预测大模型数据预处理工具 (陈歆)"""
    name = "wplm_data_preprocessing"
    description = '根据用户输入的gdsx文件路径，进行数据预处理，包括缺失值处理、异常值检测、标准化、特征工程等。得到json格式处理数据（用于大模型推理）以及用于绘图的gdsx数据文件路径'
    parameters = [{
        'name': 'raw_gdsx_file_path',
        'type': 'string',
        'description': '测井数据文件路径',
        'required': True,
    }, {
        'name': 'operations',
        'type': 'object',
        'description': '预处理操作参数，包含curves/startDepth/endDepth/curveNameStandard/curveUnitStandard/resample/wellCoordinateGenerate等',
        'required': False,
    }, {
        'name': 'download_gdsx_file_path',
        'type': 'string',
        'description': '处理后gdsx文件的存储路径，不传则默认存储在原始文件同目录下，文件名为原文件名_processed.gdsx',
        'required': False,
    }
    ]

    def run(self, params: str, **kwargs) -> str:
        """执行测井数据预处理。

        依次调用 DataPreProcess 服务的三个接口：
          1. POST /api/file/upload        — 上传原始 gdsx 文件
          2. POST /api/data/processForGDSX — 数据处理（标准化、重采样等）
          3. POST /api/file/download       — 下载处理后的 gdsx 文件

        参数:
            params (str): JSON 字符串，包含:
                - raw_gdsx_file_path (str): 原始测井数据文件路径
                - operations (object, 可选): 预处理参数，包含:
                    - curves (array): 需要获取曲线列表
                    - startDepth (number): 起始深度
                    - endDepth (number): 结束深度
                    - curveNameStandard (object): 曲线名称标准化配置
                    - curveUnitStandard (object): 曲线单位标准化配置
                    - resample (object): 重采样配置
                    - wellCoordinateGenerate (object): 井坐标生成配置
                - download_gdsx_file_path (str, 可选): 处理后gdsx文件的存储路径，不传则默认存于原始文件同目录

        返回:
            JSON 字符串，字段含义:
                - success (bool): 是否处理成功
                - dataSource (str): 原始测井数据文件路径
                - gdsx_file_path (str): 下载的处理后 gdsx 文件本地路径
                - logReqJson (str): 传给推理接口的数据体 JSON 字符串
                - message (str): 处理结果描述
        """
        params_dict = json.loads(params) if isinstance(params, str) else params
        raw_gdsx_file_path = params_dict.get('raw_gdsx_file_path', '')
        operations = params_dict.get('operations', {})
        download_gdsx_file_path = params_dict.get('download_gdsx_file_path', '')

        if not raw_gdsx_file_path or not os.path.exists(raw_gdsx_file_path):
            return json.dumps({'success': False, 'message': f'文件不存在: {raw_gdsx_file_path}'},
                              ensure_ascii=False)

        # 确定处理后文件的存储路径：优先使用用户指定的 download_gdsx_file_path
        if download_gdsx_file_path:
            gdsx_file_path = download_gdsx_file_path
        else:
            local_dir = os.path.dirname(raw_gdsx_file_path) or '.'
            base_name = os.path.splitext(os.path.basename(raw_gdsx_file_path))[0]
            gdsx_file_path = os.path.join(local_dir, f'{base_name}_processed.gdsx')

        # ---- 第1步：文件上传 /api/file/upload ----
        try:
            with open(raw_gdsx_file_path, 'rb') as f:
                upload_resp = requests.post(
                    f'{DPP_BASE_URL}/api/file/upload',
                    files={'file': (os.path.basename(raw_gdsx_file_path), f)},
                    timeout=DPP_TIMEOUT_SECONDS,
                )
            upload_resp.raise_for_status()
        except requests.RequestException as e:
            return json.dumps({'success': False, 'message': f'文件上传失败: {e}'},
                              ensure_ascii=False)

        upload_data = upload_resp.json().get('data', {})
        server_path = upload_data.get('filePath', '')

        # ---- 第2步：数据处理 /api/data/processForGDSX ----
        # 将 operations 中的参数转换为接口所需的请求体格式
        process_body = {'files': [server_path]}
        if isinstance(operations, dict):
            process_body.update(operations)

        try:
            process_resp = requests.post(
                f'{DPP_BASE_URL}/api/data/processForGDSX',
                json=process_body,
                timeout=DPP_TIMEOUT_SECONDS,
            )
            process_resp.raise_for_status()
        except requests.RequestException as e:
            return json.dumps({'success': False, 'message': f'数据处理失败: {e}'},
                              ensure_ascii=False)

        process_result = process_resp.json()
        process_data = process_result.get('data', {})

        # 提取 data 内容作为传给推理接口的 logReqJson 数据体
        log_req_json = json.dumps(process_data, ensure_ascii=False)

        # ---- 第3步：文件下载 /api/file/download ----
        try:
            download_resp = requests.post(
                f'{DPP_BASE_URL}/api/file/download',
                json={'filePath': server_path},
                timeout=DPP_TIMEOUT_SECONDS,
            )
            download_resp.raise_for_status()
        except requests.RequestException as e:
            return json.dumps({'success': False, 'message': f'文件下载失败: {e}'},
                              ensure_ascii=False)

        # 确保目标目录存在
        os.makedirs(os.path.dirname(gdsx_file_path) or '.', exist_ok=True)
        with open(gdsx_file_path, 'wb') as df:
            df.write(download_resp.content)

        return json.dumps({
            'success': True,
            'dataSource': raw_gdsx_file_path,
            'gdsx_file_path': gdsx_file_path,
            'logReqJson': log_req_json,
            'message': f'数据预处理完成，处理后gdsx文件: {gdsx_file_path}',
        }, ensure_ascii=False)
class WellLogServiceListTool(AgentScopeJsonTool):
    """测井预测模型服务列表查询工具"""
    name = "welllog_service_list"
    description = '查询当前运行中的测井预测模型服务列表，返回服务ID、模型名称、版本、创建人等信息。该工具无需传入参数，固定查询状态为"运行中"的服务。'
    SERVICE_LIST_URL = os.environ.get('WELLLOG_SERVICE_LIST_URL', '')

    parameters = {
        'type': 'object',
        'properties': {},
        'required': [],
    }

    def run(self, params: str, **kwargs) -> str:
        try:
            token = _welllog_login()
        except Exception as e:
            return json.dumps({'success': False, 'error': str(e)}, ensure_ascii=False)
        headers = {
            'Authorization': token,
            'Content-Type': 'application/json',
        }
        payload = {'name': '', 'pageNum': 1, 'pageSize': 100, 'status': '运行中'}
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        try:
            resp = requests.post(self.SERVICE_LIST_URL, data=body, headers=headers, timeout=60)
            resp.raise_for_status()
        except requests.RequestException as e:
            return json.dumps({'success': False, 'error': f'服务列表接口请求失败: {e}'}, ensure_ascii=False)
        try:
            result = json.loads(resp.text)
        except ValueError:
            return json.dumps({'success': False, 'error': '服务列表接口返回非JSON', 'rawText': resp.text[:500]}, ensure_ascii=False)
        return json.dumps(result, ensure_ascii=False)


class WellLogPredictionModelTool(AgentScopeJsonTool):
    """测井预测大模型推理工具"""
    name = "wplm_model"
    description = '使用测井预测大模型对储层参数进行同步推理预测。先调用登录接口获取 token，再携带 token 调用 encodingInferenceBySyn 推理接口，返回预测曲线、地质分层等推理结果。'
    INFERENCE_URL = os.environ.get('WELLLOG_INFERENCE_URL', '')
    parameters = [{
        'name': 'wellName',
        'type': 'string',
        'description': '井名',
        'required': True,
    },
    {
        "name": "serviceId",
        "type": "string",
        "description": "服务ID",
        "required": True,
    },
    {
        "name": "taskConfig",
        "type": "object",
        "description": "任务配置,包含分类任务(CLS)和数值任务的参数(NUM)",
        "required": True,
    },
    {
        "name": "batchSize",
        "type": "integer",
        "description": "批量大小",
        "required": True,
    },
    {
        "name": "createPeople",
        "type": "string",
        "description": "创建人（系统登陆人）",
        "required": True,
    },
    {
        "name": "logReqJson",
        "type": "object",
        "description": "数据体JSON, 从DataPreprocessingTool获取的处理结果",
        "required": True,
    },
    ]
    
    
    # parameters = {
    #     'type': 'object',
    #     'properties': {
    #         'wellName': {'type': 'string', 'description': '井名'},
    #         'serviceId': {'type': 'string', 'description': '推理服务ID'},
    #         'createPeople': {'type': 'string', 'description': '创建人', 'default': '智能体平台'},
    #         'encodingUrl': {'type': 'string', 'description': '编码服务地址，可为空', 'default': ''},
    #         'batchSize': {'type': 'integer', 'description': '批大小', 'default': 1024},
    #         'taskConfig': {
    #             'type': 'object',
    #             'description': '任务配置：CLS 为分类目标曲线列表（如 DZFC/CCHF/JSJL），NUM 为数值预测目标曲线列表（如 POR/SW/PERM/SH/SAND/LIME/DOLO/CARB/ANHY）',
    #             'properties': {
    #                 'CLS': {'type': 'array', 'items': {'type': 'string'}},
    #                 'NUM': {'type': 'array', 'items': {'type': 'string'}},
    #             },
    #         },
    #         'logReqJson': {
    #             'type': 'object',
    #             'description': '测井输入曲线数据对象，键为曲线名（如 AC/RT/GR/DEN/CNL/DEPTH 等），值为对应深度序列数组；可包含 area/logname/qxm 等元信息字段',
    #         },
    #     },
    #     'required': ['wellName', 'serviceId', 'taskConfig', 'logReqJson'],
    # }

    def run(self, params: str, **kwargs) -> str:
        """调用测井预测大模型执行单井同步推理。

        参数:
            params (str): JSON 字符串，包含:
                - wellName (str): 井名
                - serviceId (str): 推理服务ID
                - taskConfig (object): 任务配置，含分类任务(CLS)和数值任务(NUM)的参数
                - batchSize (int): 批量大小
                - createPeople (str): 创建人（系统登录人）
                - logReqJson (object): 数据体 JSON，来自 DataPreprocessingTool 返回的 logReqJson

        返回:
            JSON 字符串，字段含义（成功时为推理接口的原始响应 result）:
                - msg (str): 响应消息，如 "单井推理获取成功"
                - code (int): 响应状态码
                - data (object): 推理结果，其内部 data.data 包含:
                    - status (str): 推理状态，SUCCESS/FAILED
                    - totalNum (int): 总点数
                    - processedNum (int): 已处理点数
                    - startTime/endTime (str): 推理起止时间
                    - evaluationData.resultData: 预测曲线列表等结果数据
            失败时:
                - success (bool): False
                - error (str): 错误信息
        """
        params_dict = json.loads(params) if isinstance(params, str) else params
        try:
            token = _welllog_login()
        except Exception as e:
            return json.dumps({'success': False, 'error': str(e)}, ensure_ascii=False)
        headers = {
            'Authorization': token,
            'Content-Type': 'application/json',
        }
        body = json.dumps(params_dict, ensure_ascii=False).encode('utf-8')
        try:
            resp = requests.post(self.INFERENCE_URL, data=body, headers=headers, timeout=600)
            resp.raise_for_status()
        except requests.RequestException as e:
            return json.dumps({'success': False, 'error': f'推理接口请求失败: {e}'}, ensure_ascii=False)
        try:
            result = json.loads(resp.text)
        except ValueError:
            return json.dumps({'success': False, 'error': '推理接口返回非JSON', 'rawText': resp.text[:500]}, ensure_ascii=False)
        return json.dumps(result, ensure_ascii=False)


class PostProcessTool(AgentScopeJsonTool):
    """测井预测大模型后处理工具 """
    name = "wplm_post_process"
    description = '根据测井预测大模型推理结果，进行后处理，形成gdsx文件路径'
    parameters = [{
        'name': 'predictions',
        'type': 'object',
        'description': 'WellLogPredictionModelTool(测井预测大模型推理工具)的完整输出结果，格式与推理接口返回完全一致。'
                       '顶层字段为 msg、code、data(其中 data 内又包含 code、msg、data 三层)。'
                       'data.data 内包含 status(推理状态，SUCCESS/FAILED)、totalNum(总点数)、processedNum(已处理点数)、startTime、endTime、evaluationData(评价与结果数据)。'
                       'evaluationData.resultData 内包含 logId、ogResultList(油气层结果列表)、geoLayerList(地质分层列表)、curveList(预测曲线列表)。'
                       '输入示例：'
                       '{"msg":"单井推理获取成功","code":200,"data":{"code":200,"msg":"推理结果获取成功","data":{'
                       '"status":"SUCCESS","totalNum":842,"processedNum":842,'
                       '"startTime":"2026-08-14T09:05:28.110147+08:00","endTime":"2026-08-14T09:05:29.565782+08:00",'
                       '"evaluationData":{"modelId":"","modelName":"","version":"",'
                       '"ogResultEvaluation":true,"geoLayerEvaluation":true,"reservoirBlockingEvaluation":true,'
                       '"reservoirParameterEvaluation":true,"lithologyParameterEvaluation":true,"createPeople":"","geoName":"",'
                       '"resultData":{"logId":"","ogResultList":[],'
                       '"geoLayerList":[{"sdep":14.2,"edep":98.3,"layer":"K1z4"}],'
                       '"curveList":[{"standardName":"POR","dimension":2,'
                       '"dimension1Type":5,"dimension1Start":14.2,"dimension1End":98.3,"dimension1Step":0.1,"dimension1Length":842,"dimension1Unit":null,'
                       '"dimension2Type":4,"dimension2Start":0.0,"dimension2End":100.0,"dimension2Step":0.0,"dimension2Length":1,"dimension2Unit":null,'
                       '"dimension3Type":4,"dimension3Start":0.0,"dimension3End":0.0,"dimension3Step":0.0,"dimension3Length":1,"dimension3Unit":null,'
                       '"dimension4Type":4,"dimension4Start":0.0,"dimension4End":0.0,"dimension4Step":0.0,"dimension4Length":1,"dimension4Unit":null,'
                       '"curveData":[0.001,0.001,0.001,...]}]}}}}'
                       'curveList 共包含 9 条预测曲线，standardName 分别为 POR(孔隙度)、PERM(渗透率)、SAND(砂岩)、DOLO(白云岩)、ANHY(硬石膏)、SH(泥质)、CARB(碳酸盐岩)、LIME(石灰岩)、SW(含水饱和度)，'
                       '每条曲线的结构完全相同，curveData 数组长度等于 dimension1Length。',
        'required': True,
    }, {
        'name': 'wplm_postprocessed_gdsx_file_path',
        'type': 'string',
        'description': '后处理后的测井数据文件路径',
        'required': True,
    },]

    # ==================== 类常量 ====================

    # 质数列表，用于地质分层编码
    PRIME_LIST = [
        2, 3, 5, 7, 11, 13, 17, 19, 23, 29,
        31, 37, 41, 43, 47, 53, 59, 61, 67, 71,
        73, 79, 83, 89, 97, 101, 103, 107, 109, 113
    ]

    # 解释结论数值映射（英文）
    NUM_TO_RESULT = {
        2: "dry", 3: "water", 5: "oil-containing water",
        7: "oil with water", 11: "poor oil", 13: "oil",
        44: "gas-containing water", 46: "gas with water",
        50: "poor gas", 52: "gas", 70: "carb", 0: "unknown",
    }

    RESULT_TO_NUM = {v: k for k, v in NUM_TO_RESULT.items()}

    # 中文解释结论 → 英文映射（用于 ogResultList 中的中文结论转换）
    CN_RESULT_MAP = {
        "水层": "water",
        "干层": "dry",
        "油层": "oil",
        "差油层": "poor oil",
        "油水同层": "oil with water",
        "含油水层": "oil-containing water",
        "气层": "gas",
        "差气层": "poor gas",
        "气水同层": "gas with water",
        "含气水层": "gas-containing water",
        "煤层": "carb",
        "未知": "unknown",
    }

    # 有效解释结论数值
    VALID_NUMS = {2, 3, 5, 7, 11, 13, 44, 46, 50, 52}

    # 优先级列表（数值越大优先级越高）
    VAL_PRIORITY = [13, 52, 11, 50, 7, 46, 5, 44, 3, 2]

    # ==================== 主入口 ====================

    def run(self, params: str, **kwargs) -> str:
        """
        主入口函数：对预测结果进行后处理，写入 gdsx 文件。

        Args:
            params: JSON字符串，包含 predictions 和 wplm_postprocessed_gdsx_file_path

        Returns:
            JSON字符串，包含 success, wellName, status, wplm_postprocessed_gdsx_file_path
        """
        params_dict = json.loads(params) if isinstance(params, str) else params

        predictions = params_dict.get('predictions', {})
        gdsx_path = params_dict.get('wplm_postprocessed_gdsx_file_path', 'wplm_postprocessed.gdsx')

        # 解析推理结果
        inference_result = predictions
        if isinstance(inference_result, str):
            inference_result = json.loads(inference_result)

        try:
            # 从 msg 字段提取井名：匹配 "XXX单井" 模式，提取 XXX 作为 well_name
            msg = inference_result.get('msg', '')
            well_match = re.search(r'(.+?)单井', msg)
            well_name = well_match.group(1) if well_match else 'unknown'
        except Exception:
            well_name = 'unknown'
        status = 'success'

        # 提取 resultData
        result_data = self._extract_result_data(inference_result)
        if not result_data:
            return json.dumps({
                'success': False,
                'wellName': well_name,
                'status': status,
                'wplm_postprocessed_gdsx_file_path': '',
                'message': '无法从预测结果中提取 resultData',
            }, ensure_ascii=False)

        # 规范化：resultData 可能是 dict（单井）或 list（多井），统一转为 list
        if isinstance(result_data, dict):
            result_data = [result_data]

        # 确保 gdsx 文件存在（不覆盖已有文件，在原基础上添加曲线和表格）
        if not os.path.exists(gdsx_path):
            if not create_gdx_log(gdsx_path, overwrite_if_exist=True) or not os.path.exists(gdsx_path):
                return json.dumps({
                    'success': False,
                    'wellName': well_name,
                    'status': status,
                    'wplm_postprocessed_gdsx_file_path': '',
                    'message': '无法创建 gdsx 文件',
                }, ensure_ascii=False)

        try:
            total_curves = 0
            total_tables = 0

            for log_item in result_data:
                curve_list = log_item.get('curveList', [])
                geo_layer_list = log_item.get('geoLayerList', [])
                og_result_list = log_item.get('ogResultList', [])

                # 将 resultData 中的 DZFC 和 JSJL 数组注入 curve_list 作为合成曲线
                # DZFC: 字符串层位名 → 数值索引（1, 2, 3...），0 表示空
                # JSJL: 字符串结论名 → 使用 NUM_TO_RESULT 反向映射转数值
                dzfc_str_values = log_item.get('DZFC', [])
                jsjl_str_values = log_item.get('JSJL', [])
                dzfc_rev_map = {}
                jsjl_rev_map = {}

                if dzfc_str_values:
                    dzfc_num_map, dzfc_rev_map = self._build_string_to_num_map(dzfc_str_values)
                    dzfc_num_values = [dzfc_num_map.get(v, 0) for v in dzfc_str_values]
                    self._inject_curve(curve_list, 'DZFC', dzfc_num_values)

                if jsjl_str_values:
                    jsjl_num_values, jsjl_rev_map = self._build_jsjl_num_map(jsjl_str_values)
                    self._inject_curve(curve_list, 'JSJL', jsjl_num_values)

                curve_map = self._build_curve_map(curve_list)

                # 运行后处理，收集 pending_curves
                pending_curves = {}
                self._run_all_processing(curve_map, pending_curves)

                # 写入原始曲线（非 _SCORES 加 _AI 后缀，_SCORES 保持原名）
                # 排除注入的合成曲线 DZFC 和 JSJL（它们只在处理时内部使用）
                curves_to_write = [c for c in curve_list if c.get('standardName', '') not in ('DZFC', 'JSJL')]
                for curve in curves_to_write:
                    name = curve.get('standardName', curve.get('name', ''))
                    gdsx_name = name if name.endswith('_SCORES') else name + '_AI'
                    curve_data = curve.get('curveData', [])
                    if not curve_data:
                        continue
                    info = self._build_curve_info(gdsx_name, curve, len(curve_data))
                    if create_gdx_curve(gdsx_path, info, overwrite_if_exist=True):
                        write_gdx_curve_data_by_index(gdsx_path, gdsx_name, 0, len(curve_data), curve_data)
                total_curves += len(curves_to_write)

                # 写入后处理曲线（保持原名，最终版），排除 DZFC_P 和 JSJL（它们写入表格）
                processed_curves = self._build_processed_curve_list(curve_map, pending_curves)
                processed_curves = [c for c in processed_curves if c.get('standardName', '') not in ('DZFC_P', 'JSJL')]
                for curve in processed_curves:
                    gdsx_name = curve.get('standardName', curve.get('name', ''))
                    curve_data = curve.get('curveData', [])
                    if not curve_data:
                        continue
                    info = self._build_curve_info(gdsx_name, curve, len(curve_data))
                    if create_gdx_curve(gdsx_path, info, overwrite_if_exist=True):
                        write_gdx_curve_data_by_index(gdsx_path, gdsx_name, 0, len(curve_data), curve_data)
                total_curves += len(processed_curves)

                # 写入原始表格（加 _AI 后缀），地质分层合并连续同类型段，油气结论保持原始
                geo_ai = self._merge_table_segments(geo_layer_list, 'layer', layer_space=0.5, layer_thick=0.5) if geo_layer_list else []
                og_ai = og_result_list if og_result_list else []
                if geo_ai:
                    self._write_single_table(gdsx_path, 'GEOLAYER_AI', 'GEOLAYER',
                                             [('顶深', 'sdep', 'float'), ('底深', 'edep', 'float'), ('层位', 'layer', 'string')],
                                             geo_ai)
                if og_ai:
                    self._write_single_table(gdsx_path, 'OGRESULT_AI', 'OGRESULT',
                                             [('顶深', 'sdep', 'float'), ('底深', 'edep', 'float'), ('结论', 'result', 'string')],
                                             og_ai)
                total_tables += (1 if geo_ai else 0) + (1 if og_ai else 0)

                # 写入处理后的表格：从 DZFC_P 曲线生成地质分层表，从 JSJL 曲线生成油气结论表
                processed_tables = self._build_processed_tables(curve_map, pending_curves, dzfc_rev_map, jsjl_rev_map)
                for table_name, table_data in processed_tables.items():
                    if not table_data:
                        continue
                    table_type = 'GEOLAYER' if table_name == 'GEOLAYER' else 'OGRESULT'
                    key_field = 'layer' if table_name == 'GEOLAYER' else 'result'
                    cols = [('顶深', 'sdep', 'float'), ('底深', 'edep', 'float'), ('层位' if table_name == 'GEOLAYER' else '结论', key_field, 'string')]
                    self._write_single_table(gdsx_path, table_name, table_type, cols, table_data)
                    total_tables += 1

            return json.dumps({
                'success': True,
                'wellName': well_name,
                'status': status,
                'wplm_postprocessed_gdsx_file_path': gdsx_path,
                'message': f'后处理完成，共写入 {total_curves} 条曲线、{total_tables} 张表格。',
            }, ensure_ascii=False)

        except Exception as e:
            return json.dumps({
                'success': False,
                'wellName': well_name,
                'status': status,
                'wplm_postprocessed_gdsx_file_path': gdsx_path,
                'message': f'后处理过程出错：{str(e)}',
            }, ensure_ascii=False)

    # ==================== 辅助方法 ====================

    def _extract_result_data(self, inference_result: dict):
        """从嵌套的 JSON 结构中提取 resultData。支持 data.data.evaluationData 和 data.evaluationData 两种路径"""
        result_data = inference_result.get('resultData', None)
        if result_data is None:
            data = inference_result.get('data', {})
            # 尝试路径 data.evaluationData.resultData
            evaluation_data = data.get('evaluationData', {}) if isinstance(data, dict) else {}
            if isinstance(evaluation_data, dict) and 'resultData' in evaluation_data:
                return evaluation_data.get('resultData', None)
            # 尝试路径 data.data.evaluationData.resultData（兼容旧格式）
            data_inner = data.get('data', {}) if isinstance(data, dict) else {}
            evaluation_data = data_inner.get('evaluationData', {}) if isinstance(data_inner, dict) else {}
            result_data = evaluation_data.get('resultData', None) if isinstance(evaluation_data, dict) else None
        return result_data

    def _build_curve_map(self, curve_list: list) -> dict:
        return {c.get('standardName', c.get('name', '')): c for c in curve_list}

    def _find_curve_data(self, curve_map: dict, name: str) -> Optional[list]:
        curve = curve_map.get(name)
        return curve.get('curveData', None) if curve else None

    def _get_curve_info(self, curve_map: dict, name: str) -> Optional[dict]:
        return curve_map.get(name, None)

    def _run_all_processing(self, curve_map: dict, pending_curves: dict):
        """运行全部后处理流程，结果存入 pending_curves"""
        layer_space = 0.5
        layer_thick = 0.5

        r = self._process_geological_layering(curve_map, layer_space, layer_thick)
        if r:
            pending_curves.update(r)

        r = self._process_reservoir_parameters(curve_map)
        if r:
            pending_curves.update(r)

        r = self._process_lithology(curve_map)
        if r:
            pending_curves.update(r)

        r = self._process_interpretation_conclusions(curve_map, layer_space, layer_thick)
        if r:
            pending_curves.update(r)

    def _build_processed_curve_list(self, curve_map: dict, pending_curves: dict) -> list:
        """根据 pending_curves 构建后处理曲线列表，用于写入 gdsx"""
        processed = []
        for name, curve_data in pending_curves.items():
            template = self._find_template_curve(curve_map, name)
            if template:
                start_depth = template.get('dimension1Start', 0)
                step = template.get('dimension1Step', 0.1)
            else:
                # 从任意一条原始曲线获取模板
                any_curve = next(iter(curve_map.values()), {}) if curve_map else {}
                start_depth = any_curve.get('dimension1Start', 0)
                step = any_curve.get('dimension1Step', 0.1)

            new_curve = {
                'standardName': name,
                'name': name,
                'dimension': 2,
                'dimension1Type': 5,
                'dimension1Start': start_depth,
                'dimension1End': start_depth + (len(curve_data) - 1) * step,
                'dimension1Step': step,
                'dimension1Length': len(curve_data),
                'dimension1Unit': None,
                'dimension2Type': 4, 'dimension2Start': 0.0, 'dimension2End': 100.0,
                'dimension2Step': 0.0, 'dimension2Length': 1, 'dimension2Unit': None,
                'dimension3Type': 4, 'dimension3Start': 0.0, 'dimension3End': 0.0,
                'dimension3Step': 0.0, 'dimension3Length': 1, 'dimension3Unit': None,
                'dimension4Type': 4, 'dimension4Start': 0.0, 'dimension4End': 0.0,
                'dimension4Step': 0.0, 'dimension4Length': 1, 'dimension4Unit': None,
                'curveData': curve_data,
            }
            processed.append(new_curve)
        return processed

    def _find_template_curve(self, curve_map: dict, name: str) -> Optional[dict]:
        """查找模板曲线：先找同名原始曲线，若找不到则去掉 _P 后缀再找"""
        if name in curve_map:
            return curve_map[name]
        base = name
        for suffix in ['_P3', '_P2', '_P1', '_P']:
            if base.endswith(suffix):
                base = base[:-len(suffix)]
                break
        return curve_map.get(base, None)

    # ==================== 字符串到数值映射方法 ====================

    def _build_string_to_num_map(self, str_values: list) -> tuple:
        """将字符串列表映射为数值索引，返回 (str->num, num->str)"""
        unique = []
        for v in str_values:
            if v and v not in unique:
                unique.append(v)
        str_to_num = {v: i + 1 for i, v in enumerate(unique)}
        num_to_str = {i + 1: v for i, v in enumerate(unique)}
        return str_to_num, num_to_str

    def _build_jsjl_num_map(self, str_values: list) -> tuple:
        """将 JSJL 字符串结论映射为数值。支持中文（通过 CN_RESULT_MAP）和英文，未知值分配新编号"""
        # 构建反向映射：先英文 -> 数值
        result_to_num = {v: k for k, v in self.NUM_TO_RESULT.items()}
        # 加入中文映射：中文 -> 英文 -> 数值
        for cn_name, en_name in self.CN_RESULT_MAP.items():
            if en_name in result_to_num:
                result_to_num[cn_name] = result_to_num[en_name]

        num_values = []
        num_to_str = {}
        next_num = max(self.NUM_TO_RESULT.keys()) + 1 if self.NUM_TO_RESULT else 100

        for v in str_values:
            if v in result_to_num:
                num = result_to_num[v]
            else:
                # 未知值，分配新编号
                if v not in result_to_num:
                    result_to_num[v] = next_num
                    num_to_str[next_num] = v
                    next_num += 1
                num = result_to_num[v]
            num_values.append(num)
            if num not in num_to_str:
                num_to_str[num] = v

        # 合并标准映射和新映射
        full_num_to_str = {k: v for k, v in self.NUM_TO_RESULT.items()}
        full_num_to_str.update(num_to_str)
        return num_values, full_num_to_str

    def _inject_curve(self, curve_list: list, name: str, data: list):
        """将合成曲线注入 curve_list，使用第一条曲线的深度信息作为模板"""
        template = curve_list[0] if curve_list else {}
        start_depth = template.get('dimension1Start', 0)
        step = template.get('dimension1Step', 0.1)

        new_curve = {
            'standardName': name,
            'name': name,
            'dimension': 2,
            'dimension1Type': 5,
            'dimension1Start': start_depth,
            'dimension1End': start_depth + (len(data) - 1) * step,
            'dimension1Step': step,
            'dimension1Length': len(data),
            'dimension1Unit': None,
            'dimension2Type': 4, 'dimension2Start': 0.0, 'dimension2End': 100.0,
            'dimension2Step': 0.0, 'dimension2Length': 1, 'dimension2Unit': None,
            'dimension3Type': 4, 'dimension3Start': 0.0, 'dimension3End': 0.0,
            'dimension3Step': 0.0, 'dimension3Length': 1, 'dimension3Unit': None,
            'dimension4Type': 4, 'dimension4Start': 0.0, 'dimension4End': 0.0,
            'dimension4Step': 0.0, 'dimension4Length': 1, 'dimension4Unit': None,
            'curveData': data,
        }
        curve_list.append(new_curve)

    # ==================== gdsx 写入方法 ====================

    def _build_curve_info(self, gdsx_name: str, curve: dict, data_len: int) -> BDCurveInfo:
        """从曲线字典构建 BDCurveInfo"""
        info = BDCurveInfo()
        info.name = gdsx_name
        info.standardName = gdsx_name
        info.dimension = curve.get('dimension', 2)
        info.dimension1Type = curve.get('dimension1Type', 5)
        info.dimension1Start = curve.get('dimension1Start', 0)
        info.dimension1End = curve.get('dimension1End', 0)
        info.dimension1Step = curve.get('dimension1Step', 0.1)
        info.dimension1Length = data_len
        info.dimension1Unit = curve.get('dimension1Unit', None)
        info.dimension2Type = curve.get('dimension2Type', 4)
        info.dimension2Start = curve.get('dimension2Start', 0)
        info.dimension2End = curve.get('dimension2End', 0)
        info.dimension2Step = curve.get('dimension2Step', 0)
        info.dimension2Length = curve.get('dimension2Length', 1)
        info.dimension2Unit = curve.get('dimension2Unit', None)
        info.dimension3Type = curve.get('dimension3Type', 4)
        info.dimension3Length = curve.get('dimension3Length', 1)
        info.dimension4Type = curve.get('dimension4Type', 4)
        info.dimension4Length = curve.get('dimension4Length', 1)
        return info

    def _write_single_table(self, gdsx_path: str, table_name: str, table_type: str,
                            columns: list, data: list):
        """写入单张表格到 gdsx（直接使用 h5py，避免 pygdsx 的 GBK 编码问题）"""
        f = h5py.File(gdsx_path, 'r+')
        key = get_the_only_log_group(f)
        if not key:
            f.close()
            return
        log_group = f[key]

        if 'table' not in log_group:
            tables = log_group.create_group('table')
        else:
            tables = log_group['table']

        # 如果表已存在，先删除
        if table_name in tables:
            del tables[table_name]

        table_group = tables.create_group(table_name)

        # 写入 meta（兼容 pygdsx 的 BDTableInfo 格式）
        table_meta = {
            'id': None, 'tableName': table_name, 'tableType': table_type,
            'tableAliasName': table_name, 'owner': None, 'catalog': None,
            'ownerId': None, 'addDate': None,
            'reserve1': None, 'reserve2': None, 'reserve3': None,
            'reserve4': None, 'reserve5': None, 'reserve6': None,
            'columns': [{'alias': alias, 'name': name, 'type': typ, 'allowNull': True}
                        for alias, name, typ in columns],
        }
        table_group.create_dataset('meta', data=json_object2numpy(table_meta))

        # 写入 data
        table_group.create_dataset('data', data=json_object2numpy(data))

        f.close()

    def _build_processed_tables(self, curve_map: dict, pending_curves: dict,
                               dzfc_rev_map: dict, jsjl_rev_map: dict) -> dict:
        """从处理后的曲线生成表格数据，返回 {表名: [行数据列表]}"""
        tables = {}
        step = self._get_depth_step(curve_map)
        start_depth = self._get_start_depth(curve_map)

        # 从 DZFC_P 曲线生成地质分层表
        if 'DZFC_P' in pending_curves:
            dzfc_data = pending_curves['DZFC_P']
            geo_table = self._curve_to_geo_layer_table(dzfc_data, step, start_depth, dzfc_rev_map)
            if geo_table:
                geo_table = self._merge_table_segments(geo_table, 'layer', layer_space=0.5, layer_thick=0.5)
                tables['GEOLAYER'] = geo_table

        # 从 JSJL 曲线生成油气结论表
        if 'JSJL' in pending_curves:
            og_data = pending_curves['JSJL']
            og_table = self._curve_to_og_result_table(og_data, step, start_depth, jsjl_rev_map)
            if og_table:
                og_table = self._merge_table_segments(og_table, 'result', layer_space=0.5, layer_thick=0.5)
                tables['OGRESULT'] = og_table

        return tables

    def _get_depth_step(self, curve_map: dict) -> float:
        """从曲线列表中获取深度步长"""
        for c in curve_map.values():
            step = c.get('dimension1Step', 0.1)
            if step and step > 0:
                return step
        return 0.1

    def _get_start_depth(self, curve_map: dict) -> float:
        """从曲线列表中获取起始深度"""
        for c in curve_map.values():
            return c.get('dimension1Start', 0)
        return 0

    def _curve_to_geo_layer_table(self, curve_data: list, step: float, start_depth: float, rev_map: dict) -> list:
        """将 DZFC 曲线数据转换为地质分层表 [{sdep, edep, layer}]。rev_map: {数值: 层位名字符串}"""
        if not curve_data:
            return []
        table = []
        seg_start = 0
        current_val = int(curve_data[0])

        for i in range(1, len(curve_data)):
            if int(curve_data[i]) != current_val:
                if current_val != 0:
                    layer_name = rev_map.get(current_val, str(current_val))
                    table.append({
                        'sdep': round(start_depth + seg_start * step, 3),
                        'edep': round(start_depth + i * step, 3),
                        'layer': layer_name,
                    })
                seg_start = i
                current_val = int(curve_data[i])

        if current_val != 0:
            layer_name = rev_map.get(current_val, str(current_val))
            table.append({
                'sdep': round(start_depth + seg_start * step, 3),
                'edep': round(start_depth + len(curve_data) * step, 3),
                'layer': layer_name,
            })
        return table

    def _merge_table_segments(self, table: list, key_field: str,
                               layer_space: float = 0.5, layer_thick: float = 0.5) -> list:
        """合并表格中相邻同类型段（间隙 <= layer_space）并剔除薄层（厚度 <= layer_thick）。
        处理重叠段：同类型合并，不同类型取先出现的。"""
        if not table:
            return []

        rows = sorted(table, key=lambda r: r['sdep'])

        changed = True
        while changed:
            changed = False

            # 1. 处理重叠：同类型合并，不同类型取先出现的
            deduped = []
            for row in rows:
                if not deduped:
                    deduped.append(dict(row))
                    continue
                last = deduped[-1]
                if row['sdep'] < last['edep']:
                    if last[key_field] == row[key_field]:
                        last['edep'] = max(last['edep'], row['edep'])
                        changed = True
                    # 不同类型：丢弃重叠部分（保留先出现的）
                    continue
                deduped.append(dict(row))
            rows = deduped

            # 2. 合并相邻同类型段（间隙 <= layer_space）
            merged = []
            for row in rows:
                if not merged:
                    merged.append(dict(row))
                    continue
                last = merged[-1]
                gap = row['sdep'] - last['edep']
                if last[key_field] == row[key_field] and gap <= layer_space:
                    last['edep'] = row['edep']
                    changed = True
                else:
                    merged.append(dict(row))
            rows = merged

            # 3. 剔除薄层并填充（厚度 <= layer_thick），前后段同类型时合并
            if len(rows) >= 3:
                result = [dict(rows[0])]
                for i in range(1, len(rows) - 1):
                    thick = rows[i]['edep'] - rows[i]['sdep']
                    if thick <= layer_thick:
                        # 填充薄层间隙
                        result[-1]['edep'] = rows[i + 1]['edep']
                        changed = True
                    else:
                        result.append(dict(rows[i]))
                result.append(dict(rows[-1]))
                rows = result

        return rows

    def _curve_to_og_result_table(self, curve_data: list, step: float, start_depth: float, rev_map: dict) -> list:
        """将 JSJL 曲线数据转换为油气结论表 [{sdep, edep, result}]。rev_map: {数值: 结论名字符串}"""
        if not curve_data:
            return []
        table = []
        seg_start = 0
        current_val = round(curve_data[0])

        for i in range(1, len(curve_data)):
            val = round(curve_data[i])
            if val != current_val:
                result_name = rev_map.get(current_val, self.NUM_TO_RESULT.get(current_val, 'unknown'))
                if current_val != 0:
                    table.append({
                        'sdep': round(start_depth + seg_start * step, 3),
                        'edep': round(start_depth + i * step, 3),
                        'result': result_name,
                    })
                seg_start = i
                current_val = val

        result_name = rev_map.get(current_val, self.NUM_TO_RESULT.get(current_val, 'unknown'))
        if current_val != 0:
            table.append({
                'sdep': round(start_depth + seg_start * step, 3),
                'edep': round(start_depth + len(curve_data) * step, 3),
                'result': result_name,
            })
        return table

    # ==================== 1. 地质分层处理 (dzfc) ====================

    def _process_geological_layering(self, curve_map: dict, layer_space: float, layer_thick: float) -> dict:
        dzfc_data = self._find_curve_data(curve_map, 'DZFC')
        dzfc_scores = self._find_curve_data(curve_map, 'DZFC_SCORES')
        if dzfc_data is None or dzfc_scores is None or len(dzfc_data) != len(dzfc_scores):
            return {}

        processed_dzfc = self._sliding_window_mode(dzfc_data, window_size=100, step_size=25)
        processed_score = self._threshold_process(dzfc_scores)
        groups = self._calculate_product_sum_groups(processed_dzfc, processed_score)
        merged_groups = self._merge_small_groups(groups)

        final_groups = []
        if merged_groups:
            final_groups.append(merged_groups[0])
            for g in merged_groups[1:]:
                last = final_groups[-1]
                if int(g['dzfc_value']) == int(last['dzfc_value']):
                    last['end_index'] = g['end_index']
                else:
                    final_groups.append(g)

        point_count = len(dzfc_data)
        output_data = [0.0] * point_count
        for g in final_groups:
            for i in range(g['start_index'], g['end_index'] + 1):
                output_data[i] = g['dzfc_value']

        return {'DZFC_P': output_data}

    def _sliding_window_mode(self, data: list, window_size: int = 100, step_size: int = 25) -> list:
        if not data:
            return []
        result = data[:]
        half_window = window_size // 2
        padded = [data[0]] * half_window + data + [data[-1]] * half_window
        data_size = len(data)

        for i in range(0, data_size, step_size):
            start = i
            end = min(i + window_size - 1, len(padded) - 1)
            window_vals = padded[start:end + 1]
            counter = Counter(int(v) for v in window_vals)
            mode = counter.most_common(1)[0][0]
            fill_end = min(i + step_size, data_size)
            for k in range(i, fill_end):
                result[k] = float(mode)

        return result

    def _threshold_process(self, data: list) -> list:
        result = []
        for value in data:
            if value >= 0.85:
                result.append(1.0)
            elif value >= 0.5:
                result.append(0.8)
            else:
                result.append(0.0)
        return result

    def _calculate_product_sum_groups(self, dzfc: list, score: list) -> list:
        results = []
        if not dzfc:
            return results

        start_index = 0
        current_value = dzfc[0]
        product_sum = dzfc[0] * score[0]

        for i in range(1, len(dzfc)):
            if int(dzfc[i]) != int(current_value):
                group_len = i - start_index
                results.append({
                    'start_index': start_index, 'end_index': i - 1,
                    'product_sum': product_sum / max(group_len, 1),
                    'dzfc_value': current_value,
                })
                start_index = i
                current_value = dzfc[i]
                product_sum = dzfc[i] * score[i]
            else:
                product_sum += dzfc[i] * score[i]

        last_len = len(dzfc) - start_index
        results.append({
            'start_index': start_index, 'end_index': len(dzfc) - 1,
            'product_sum': product_sum / max(last_len, 1),
            'dzfc_value': current_value,
        })
        return results

    def _merge_small_groups(self, groups: list) -> list:
        if not groups:
            return []

        temp = groups[:]
        if len(temp) >= 2 and int(temp[0]['dzfc_value']) == 0:
            temp[1]['start_index'] = temp[0]['start_index']
            temp.pop(0)

        if not temp:
            return []

        result = [temp[0]]
        for i in range(1, len(temp)):
            current = temp[i]
            last = result[-1]
            if current['product_sum'] < last['product_sum']:
                last['end_index'] = current['end_index']
            else:
                result.append(current)

        return result

    # ==================== 2. 储层参数处理 ====================

    def _process_reservoir_parameters(self, curve_map: dict) -> dict:
        result = {}
        for base_name in ['POR', 'SW', 'PERM']:
            r = self._process_single_curve_filter(curve_map, base_name, is_reservoir=True)
            if r:
                result.update(r)
        return result

    # ==================== 3. 岩性处理 ====================

    def _process_lithology(self, curve_map: dict) -> dict:
        result = {}
        for base_name in ['SH', 'SAND', 'CARB', 'LIME', 'DOLO', 'ANHY']:
            r = self._process_single_curve_filter(curve_map, base_name, is_reservoir=False)
            if r:
                result.update(r)
        return result

    def _process_single_curve_filter(self, curve_map: dict, base_name: str, is_reservoir: bool) -> dict:
        curve_name = base_name
        score_name = base_name + '_SCORES'

        curve_data = self._find_curve_data(curve_map, curve_name)
        score_data = self._find_curve_data(curve_map, score_name)

        if curve_data is None or score_data is None or len(curve_data) != len(score_data):
            return {}

        clean_data = [0.0001 if (math.isnan(v) or math.isinf(v) or v < 0) else v for v in curve_data]

        interp_data = self._interpolate_low_confidence(clean_data, score_data, threshold=0.6, max_consecutive=15)
        mid_data = self._median_filter(interp_data, window_size=5)
        self._clamp_values(mid_data, base_name, is_reservoir)
        avg_data = self._mean_filter(mid_data, window_size=3)
        self._clamp_values(avg_data, base_name, is_reservoir)

        return {
            base_name: avg_data,
        }

    def _interpolate_low_confidence(self, data: list, scores: list,
                                     threshold: float = 0.6, max_consecutive: int = 15) -> list:
        result = data[:]
        size = len(data)
        consecutive_count = 0
        start_index = -1

        for i in range(size):
            if scores[i] < threshold:
                if start_index == -1:
                    start_index = i
                consecutive_count += 1
            else:
                if start_index != -1 and consecutive_count <= max_consecutive:
                    left_idx = start_index - 1
                    right_idx = start_index + consecutive_count
                    if left_idx >= 0 and right_idx < size:
                        left_val = max(result[left_idx], 0.0001)
                        right_val = max(result[right_idx], 0.0001)
                        for j in range(start_index, start_index + consecutive_count):
                            ratio = float(j - left_idx) / (right_idx - left_idx)
                            result[j] = max(left_val + ratio * (right_val - left_val), 0.0000001)
                consecutive_count = 0
                start_index = -1

        if start_index != -1 and consecutive_count <= max_consecutive:
            left_idx = start_index - 1
            right_idx = start_index + consecutive_count
            if left_idx >= 0 and right_idx < size:
                left_val = max(result[left_idx], 0.0001)
                right_val = max(result[right_idx], 0.0001)
                for j in range(start_index, start_index + consecutive_count):
                    ratio = float(j - left_idx) / (right_idx - left_idx)
                    result[j] = max(left_val + ratio * (right_val - left_val), 0.0000001)

        return result

    def _median_filter(self, data: list, window_size: int = 5) -> list:
        if not data:
            return []
        result = data[:]
        half = window_size // 2
        size = len(data)
        for i in range(half, size - half):
            result[i] = sorted(data[i - half:i + half + 1])[half]
        return result

    def _mean_filter(self, data: list, window_size: int = 3) -> list:
        if not data:
            return []
        result = data[:]
        half = window_size // 2
        size = len(data)
        for i in range(half, size - half):
            result[i] = sum(data[i - half:i + half + 1]) / window_size
        return result

    def _clamp_values(self, data: list, base_name: str, is_reservoir: bool):
        if is_reservoir:
            if base_name == 'POR':
                for i in range(len(data)):
                    data[i] = max(0.0, min(data[i], 40.0))
            elif base_name == 'SW':
                for i in range(len(data)):
                    data[i] = max(0.0, min(data[i], 100.0))
            elif base_name == 'PERM':
                for i in range(len(data)):
                    if data[i] < 0.0001 or data[i] > 1000:
                        data[i] = 0.0001
        else:
            for i in range(len(data)):
                data[i] = max(0.0, data[i])

    # ==================== 4. 解释结论处理 ====================

    def _process_interpretation_conclusions(self, curve_map: dict, layer_space: float, layer_thick: float) -> dict:
        base_name = 'JSJL'
        score_name = 'JSJL_SCORES'

        curve_data = self._find_curve_data(curve_map, base_name)
        score_data = self._find_curve_data(curve_map, score_name)

        if curve_data is None or score_data is None or len(curve_data) != len(score_data):
            return {}

        curve_info = self._get_curve_info(curve_map, base_name)
        step = curve_info.get('dimension1Step', 0.1) if curve_info else 0.1

        interp_data = self._interpolate_ogresult_low_confidence(curve_data, score_data)

        current_data = self._median_filter(interp_data, window_size=7)
        for i in range(len(current_data)):
            current_data[i] = round(current_data[i])

        # 追加：中值滤波后先剔除一次孤立薄层
        # thin_layer_points = round(layer_thick / step) if step > 0 else 5
        # current_data = self._remove_isolated_thin_layers(current_data, thin_layer_points)

        current_data = self._fix_segment_ends(current_data)

        max_gap_points = round(layer_space / step) if step > 0 else 5
        current_data = self._merge_small_gaps(current_data, max_gap_points)

        current_data = self._split_long_dry_water_segments(current_data, threshold=30)

        thin_layer_points = round(layer_thick / step) if step > 0 else 5
        current_data = self._remove_isolated_thin_layers(current_data, thin_layer_points)

        filter_data = self._sliding_window_weighted_filter(current_data)

        return {
            base_name: filter_data[:],
        }

    def _interpolate_ogresult_low_confidence(self, data: list, scores: list) -> list:
        result = data[:]
        valid_segments = self._find_valid_segments(result)

        for seg_start, seg_end in valid_segments:
            low_score_start = -1
            low_score_len = 0

            for i in range(seg_start, seg_end + 1):
                curr_score = scores[i]
                curr_val = round(result[i])

                if curr_score < 0.5 and curr_val in self.VALID_NUMS:
                    if low_score_start == -1:
                        low_score_start = i
                    low_score_len += 1
                else:
                    if low_score_start != -1 and low_score_len <= 15:
                        left_ref = -1
                        right_ref = -1
                        for k in range(low_score_start - 1, seg_start - 1, -1):
                            if scores[k] >= 0.5 and round(result[k]) in self.VALID_NUMS:
                                left_ref = k
                                break
                        for k in range(low_score_start + low_score_len, seg_end + 1):
                            if scores[k] >= 0.5 and round(result[k]) in self.VALID_NUMS:
                                right_ref = k
                                break

                        if left_ref != -1 and right_ref != -1:
                            left_val = round(result[left_ref])
                            right_val = round(result[right_ref])
                            mid_point = left_ref + (right_ref - left_ref) // 2
                            for j in range(low_score_start, mid_point + 1):
                                result[j] = left_val
                            for j in range(mid_point + 1, right_ref):
                                result[j] = right_val
                        elif left_ref != -1:
                            val = round(result[left_ref])
                            for j in range(low_score_start, low_score_start + low_score_len):
                                result[j] = val
                        elif right_ref != -1:
                            val = round(result[right_ref])
                            for j in range(low_score_start, low_score_start + low_score_len):
                                result[j] = val

                    low_score_start = -1
                    low_score_len = 0

        return result

    def _find_valid_segments(self, data: list) -> list:
        segments = []
        seg_start = -1
        for i, val in enumerate(data):
            if round(val) in self.VALID_NUMS:
                if seg_start == -1:
                    seg_start = i
            else:
                if seg_start != -1:
                    segments.append((seg_start, i - 1))
                    seg_start = -1
        if seg_start != -1:
            segments.append((seg_start, len(data) - 1))
        return segments

    def _fix_segment_ends(self, data: list) -> list:
        result = data[:]
        segments = self._find_valid_segments(result)
        for seg_start, seg_end in segments:
            seg_len = seg_end - seg_start + 1
            if seg_len < 3:
                continue
            # C++: 统计前3个值，取众数（不提前过滤，与 C++ getMainValue 一致）
            head_vals = [round(result[i]) for i in range(seg_start, seg_start + 3)]
            result[seg_start] = self._get_priority_mode(head_vals)
            tail_vals = [round(result[i]) for i in range(seg_end - 2, seg_end + 1)]
            result[seg_end] = self._get_priority_mode(tail_vals)
        return result

    def _get_priority_mode(self, values: list) -> int:
        if not values:
            return 2
        counter = Counter(values)
        max_count = max(counter.values())
        candidates = [v for v, c in counter.items() if c == max_count]
        for pri_val in self.VAL_PRIORITY:
            if pri_val in candidates:
                return pri_val
        return candidates[0] if candidates else 2

    def _merge_small_gaps(self, data: list, max_gap_points: int) -> list:
        result = data[:]
        segments = self._find_valid_segments(result)
        for i in range(len(segments) - 1):
            curr_seg = segments[i]
            next_seg = segments[i + 1]
            gap_start = curr_seg[1] + 1
            gap_end = next_seg[0] - 1
            gap_len = gap_end - gap_start + 1
            if gap_len > 0 and gap_len <= max_gap_points:
                fill_val = round(result[curr_seg[1]])
                for j in range(gap_start, gap_end + 1):
                    result[j] = fill_val
        return result

    def _split_long_dry_water_segments(self, data: list, threshold: int = 30) -> list:
        result = data[:]
        segments = self._find_valid_segments(result)
        long_segments = []
        for seg_start, seg_end in segments:
            seg_len = seg_end - seg_start + 1
            if seg_len > threshold:
                # C++: 只拆分纯干层(2)/水层(3)的长段
                is_all_dry_or_water = all(
                    round(result[i]) in (2, 3) for i in range(seg_start, seg_end + 1)
                )
                if is_all_dry_or_water:
                    long_segments.append((seg_start, seg_end))

        for long_start, long_end in long_segments:
            for seg_start, seg_end in segments:
                if seg_end + 1 == long_start:
                    for j in range(seg_end + 1, long_start):
                        result[j] = 0
                    break
                if seg_start - 1 == long_end:
                    for j in range(long_end + 1, seg_start):
                        result[j] = 0
                    break
        return result

    def _remove_isolated_thin_layers(self, data: list, thin_layer_points: int) -> list:
        result = data[:]
        segments = self._find_valid_segments(result)
        check_range = thin_layer_points
        for seg_start, seg_end in segments:
            seg_len = seg_end - seg_start + 1
            if seg_len > thin_layer_points:
                continue
            is_isolated = True
            front_start = max(0, seg_start - check_range)
            for i in range(front_start, seg_start):
                if round(result[i]) != 0:
                    is_isolated = False
                    break
            if is_isolated:
                back_end = min(len(result) - 1, seg_end + check_range)
                for i in range(seg_end + 1, back_end + 1):
                    if round(result[i]) != 0:
                        is_isolated = False
                        break
            if is_isolated:
                for i in range(seg_start, seg_end + 1):
                    result[i] = 0
        return result

    def _sliding_window_weighted_filter(self, data: list) -> list:
        result = data[:]
        segments = self._find_valid_segments(result)
        window_size = 13
        half_window = window_size // 2
        min_seg_length = 13
        seg_length_threshold = 19
        short_seg_weight = 3.0
        normal_weight = 1.0

        for seg_start, seg_end in segments:
            seg_len = seg_end - seg_start + 1
            if seg_len <= min_seg_length:
                continue
            is_short_seg = (seg_len <= seg_length_threshold)

            for i in range(seg_start, seg_end + 1):
                win_start = max(seg_start, i - half_window)
                win_end = min(seg_end, i + half_window)
                center_pos = i - win_start
                window_vals = [round(result[j]) for j in range(win_start, win_end + 1)]

                score_map = {}
                for idx, val in enumerate(window_vals):
                    if val not in self.VALID_NUMS:
                        continue
                    dist_to_center = abs(idx - center_pos)
                    weight = short_seg_weight if (is_short_seg and dist_to_center <= 1) else normal_weight
                    score_map[val] = score_map.get(val, 0.0) + weight

                if score_map:
                    max_score = max(score_map.values())
                    candidates = [v for v, s in score_map.items() if s == max_score]
                    mode_val = candidates[0]
                    for pri_val in self.VAL_PRIORITY:
                        if pri_val in candidates:
                            mode_val = pri_val
                            break
                    result[i] = mode_val

        return result
        
###################################### TODO：曲线完整性工具合并 ######################################
from cnlc_agent.wplm.data_analysis_utils.gdsx_service import (   run_completeness, run_crossplot, run_expansion, run_standardize, run_ogresult_distribution, run_ogresult_crossplot, DEFAULT_OUTPUT_DIR,)


class GdsxCompletenessTool(AgentScopeJsonTool):
    """输入 GDSX 文件路径，计算数据完整度并输出结果 JSON 文件与 echarts 柱状图 option。"""
    name = "gdsx_completeness"
    description = (
        "GDSX测井数据完整度检测。输入 GDSX 数据文件路径，按曲线统计有效/无效/缺失"
        "点数与完整率，输出结构化结果 JSON 文件(默认落盘到 gdsx_parser/outputs)；"
        "默认统计 9 条标准曲线(AC/CAL/CNL/DEN/GR/PE/RT/RXO/SP)，可用 curves 参数"
        "选择其中若干条；如需各曲线完整率柱状图，可设置 need_chart=true，返回结果"
        "中附带 echarts_option 供 echarts 组件渲染。"
    )
    parameters = [
        {
            "name": "gdsx_path",
            "type": "string",
            "description": "GDSX 数据文件路径（必填，需为实际存在的 .gdsx 文件）",
            "required": True,
        },
        {
            "name": "output_dir",
            "type": "string",
            "description": "结果输出目录(可选)，默认使用项目内 gdsx_parser/outputs",
            "required": False,
        },
        {
            "name": "need_chart",
            "type": "bool",
            "description": "是否生成 echarts 柱状图 option(可选)，默认 false 不绘图",
            "required": False,
        },
        {
            "name": "curves",
            "type": "array",
            "description": "需要统计的曲线名列表(可选)。默认 9 条标准曲线，可用此参数选子集",
            "required": False,
        },
        {
            "name": "save_files",
            "type": "bool",
            "description": "是否将结果/option JSON 落盘(可选，默认 false)。仅本地测试需要，部署后不用",
            "required": False,
        },
    ]

    def run(self, p: str, **kwargs) -> str:
        params = self._verify_json_format_args(p or "{}")
        gdsx_path = params.get("gdsx_path", "")
        output_dir = params.get("output_dir") or None
        need_chart = bool(params.get("need_chart", False))
        save_files = bool(params.get("save_files", False))
        curves = params.get("curves") or None

        result = run_completeness(
            gdsx_path=gdsx_path,
            output_dir=output_dir,
            need_chart=need_chart,
            save_files=save_files,
            curves=curves,
        )
        return json.dumps(result, ensure_ascii=False, default=str)


class GdsxExpansionTool(AgentScopeJsonTool):
    """输入 GDSX 文件路径，计算井径扩/缩径率并输出结果 JSON 文件与 echarts 折线图 option。"""
    name = "gdsx_expansion"
    description = (
        "GDSX测井数据井径扩径/缩径率计算。输入 GDSX 数据文件路径，依据井次信息中的"
        "钻头配置(BD/DRILLBITDEPTH)与井径(CAL)曲线，按钻头分段、每段 30 米区间计算"
        "平均井径相对钻头直径的扩缩径率；输出结构化结果 JSON 文件，并在返回结果中"
        "附带 echarts_option（深度×扩缩径率阶梯折线图，按钻头分段），可直接交给"
        "echarts 组件渲染。"
    )
    parameters = [
        {
            "name": "gdsx_path",
            "type": "string",
            "description": "GDSX 数据文件路径（必填，需为实际存在的 .gdsx 文件）",
            "required": True,
        },
        {
            "name": "output_dir",
            "type": "string",
            "description": "结果输出目录(可选)，默认使用项目内 gdsx_parser/outputs",
            "required": False,
        },
        {
            "name": "need_chart",
            "type": "bool",
            "description": "是否生成 echarts 折线图 option(可选)，默认 true 绘图",
            "required": False,
        },
        {
            "name": "save_files",
            "type": "bool",
            "description": "是否将结果/option JSON 落盘(可选，默认 false)。仅本地测试需要，部署后不用",
            "required": False,
        },
    ]

    def run(self, p: str, **kwargs) -> str:
        params = self._verify_json_format_args(p or "{}")
        gdsx_path = params.get("gdsx_path", "")
        output_dir = params.get("output_dir") or None
        need_chart = bool(params.get("need_chart", True))
        save_files = bool(params.get("save_files", False))

        result = run_expansion(
            gdsx_path=gdsx_path,
            output_dir=output_dir,
            need_chart=need_chart,
            save_files=save_files,
        )
        return json.dumps(result, ensure_ascii=False, default=str)


class GdsxCrossplotTool(AgentScopeJsonTool):
    """输入 GDSX 文件路径，输出密度-中子/声波-中子交会图 echarts option。"""
    name = "gdsx_crossplot"
    description = (
        "GDSX测井数据交会图分析。输入 GDSX 数据文件路径，采集同一深度点的"
        "中子(CNL)与密度(DEN)、中子(CNL)与声波(AC)数据，按绘图模板固定轴刻度"
        "（密度-中子：横轴中子-10~60%、纵轴密度1.8~3.0 g/cm3反向刻度；"
        "声波-中子：横轴中子-10~60%、纵轴声波100~500 μs/m），"
        "输出密度-中子与声波-中子两张 echarts_option（各含主散点图、"
        "顶部频数直方图、右侧频数直方图三块拼接面板及色标条），"
        "通过 echarts_option 字典（density_neutron / acoustic_neutron）返回，"
        "可直接交给 echarts 组件渲染。AC 曲线缺失时声波-中子图散点为空。"
    )
    parameters = [
        {
            "name": "gdsx_path",
            "type": "string",
            "description": "GDSX 数据文件路径（必填，需为实际存在的 .gdsx 文件）",
            "required": True,
        },
        {
            "name": "output_dir",
            "type": "string",
            "description": "结果输出目录(可选)，默认使用项目内 gdsx_parser/outputs",
            "required": False,
        },
        {
            "name": "need_chart",
            "type": "bool",
            "description": "是否生成 echarts 散点图 option(可选)，默认 true 绘图",
            "required": False,
        },
        {
            "name": "save_files",
            "type": "bool",
            "description": "是否将结果/option JSON 落盘(可选，默认 false)。仅本地测试需要，部署后不用",
            "required": False,
        },
    ]

    def run(self, p: str, **kwargs) -> str:
        params = self._verify_json_format_args(p or "{}")
        gdsx_path = params.get("gdsx_path", "")
        output_dir = params.get("output_dir") or None
        need_chart = bool(params.get("need_chart", True))
        save_files = bool(params.get("save_files", False))

        result = run_crossplot(
            gdsx_path=gdsx_path,
            output_dir=output_dir,
            need_chart=need_chart,
            save_files=save_files,
        )
        return json.dumps(result, ensure_ascii=False, default=str)


class GdsxPostProcessTool(AgentScopeJsonTool):
    """输入一个 GDSX 文件路径，依次运行油气结论储层分布统计、油气结论交会散点图，输出每张图的 echarts option。"""
    name = "gdsx_post_process"
    description = (
        "GDSX测井数据后处理统一分析。输入一个 GDSX 数据文件路径，按固定顺序依次运行"
        "两个分析工具：①油气结论储层分布统计(各储层结论累计厚度柱状图) → ②油气结论"
        "交会散点图(孔隙度-渗透率、孔隙度-含水饱和度、渗透率-含水饱和度三张散点图，"
        "PERM 取对数)。每个工具返回各自的 echarts_option，并分别落盘为 option JSON 文件；"
        "本工具汇总返回所有 option 文件路径与 echarts_option 字典，可直接用于 echarts "
        "组件渲染绘图。若 GDSX 无油气结论(OGRESULT)表格则返回空结果提示。"
    )
    parameters = [
        {
            "name": "gdsx_path",
            "type": "string",
            "description": "GDSX 数据文件路径（必填，需为实际存在的 .gdsx 文件）",
            "required": True,
        },
        {
            "name": "output_dir",
            "type": "string",
            "description": "结果输出目录(可选)，默认使用项目内 gdsx_parser/outputs",
            "required": False,
        },
        {
            "name": "save_files",
            "type": "bool",
            "description": "是否将各工具结果/option JSON 落盘(可选，默认 false)。仅本地测试需要，部署后不用",
            "required": False,
        },
    ]

    def run(self, p: str, **kwargs) -> str:
        params = self._verify_json_format_args(p or "{}")
        gdsx_path = params.get("gdsx_path", "")
        output_dir = params.get("output_dir") or None
        # 固定需要出图
        need_chart = True
        save_files = bool(params.get("save_files", False))

        tools = [
            ("ogresult", run_ogresult_distribution, {"gdsx_path": gdsx_path, "output_dir": output_dir, "need_chart": need_chart, "save_files": save_files}),
            ("ogresult_crossplot", run_ogresult_crossplot, {"gdsx_path": gdsx_path, "output_dir": output_dir, "need_chart": need_chart, "save_files": save_files}),
        ]

        results = {}
        option_files = []
        messages = []
        overall_success = True
        for key, func, kwargs in tools:
            res = func(**kwargs)
            results[key] = res
            if not res.get("success"):
                overall_success = False
                messages.append(f"{key}: {res.get('message')}")
                continue
            # 收集 option（忽略是否成功，保留 message），直接放入原始 option 对象
            messages.append(f"{key}: {res.get('message')}")
            opt = res.get("echarts_option")
            if isinstance(opt, dict) and opt:
                if key == "ogresult_crossplot":
                    # 交会散点图是三张图的字段字典，逐张放入
                    for sub in ("por_perm", "por_sw", "perm_sw"):
                        sub_opt = opt.get(sub)
                        if sub_opt:
                            option_files.append(sub_opt)
                else:
                    option_files.append(opt)

        result = {
            "success": overall_success,
            "gdsx_path": gdsx_path,
            "output_dir": str(output_dir or DEFAULT_OUTPUT_DIR),
            "option_files": option_files,
            "message": "；".join(messages),
        }
        return json.dumps(result, ensure_ascii=False, default=str)


class GdsxStandardizeTool(AgentScopeJsonTool):
    name = "gdsx_standardize"
    """对 GDSX 文件的曲线名做标准化命名，输出一个新的 GDSX 副本。

    输入源 GDSX 路径与目标文件名，按标准曲线映射字典逐个标准化：
    若曲线名已是标准名则不改；若是某标准名的别名，则按映射表别名优先顺序把
    「该标准名尚未被占用时的首个别名」改名为标准名，原名写入该曲线 description。
    完成后返回新的 GDSX 文件绝对路径。
    """
    description = (
        "GDSX测井数据曲线名标准化。输入源 GDSX 文件路径与目标文件名，依据标准曲线"
        "映射字典(STANDARD_CURVE_DICT)逐条曲线标准化：已是标准名的曲线保持不变；"
        "若是某标准名的别名，则按映射表别名优先顺序，将该标准名的首个可用别名改名为"
        "标准名，并把原名写入该曲线 description 字段；同一标准名的后续别名因标准名"
        "已被占用而不再改名。输出一个新的 GDSX 副本文件并返回其绝对路径。"
    )
    parameters = [
        {
            "name": "read_gdsx_file_path",
            "type": "string",
            "description": "源 GDSX 数据文件绝对路径（必填，需为存在的 .gdsx 文件）",
            "required": True,
        },
        {
            "name": "write_gdsx_file_folder",
            "type": "string",
            "description": "新 GDSX 文件所在目录的绝对路径（必填）",
            "required": True,
        },
        {
            "name": "write_gdsx_file_name",
            "type": "string",
            "description": "新 GDSX 文件名，需以 .gdsx 结尾（必填）",
            "required": True,
        },
        {
            "name": "standard_curve_dict",
            "type": "object",
            "description": "曲线映射关系字典（标准名→别名列表，可选）。默认使用 config.STANDARD_CURVE_DICT",
            "required": False,
        },
    ]

    def run(self, p: str, **kwargs) -> str:
        params = self._verify_json_format_args(p or "{}")
        read_path = params.get("read_gdsx_file_path", "")
        folder = params.get("write_gdsx_file_folder", "")
        name = params.get("write_gdsx_file_name", "")
        std_dict = params.get("standard_curve_dict") or None

        result = run_standardize(
            read_gdsx_file_path=read_path,
            write_gdsx_file_folder=folder,
            write_gdsx_file_name=name,
            standard_curve_dict=std_dict,
        )
        return json.dumps(result, ensure_ascii=False, default=str)


class GdsxOgResultTool(AgentScopeJsonTool):
    """输入 GDSX 文件路径，统计油气结论(OGRESULT)表格的储层分布并输出 echarts 柱状图 option。"""
    name = "gdsx_ogresult_distribution"
    description = (
        "GDSX测井数据油气结论储层分布统计。输入 GDSX 数据文件路径，读取油气结论表格"
        "(键名自动兼容 OGRESULT 与中文名「油气结论」)，以结论(result)分类别统计各储层"
        "层段的段数、累计厚度与平均厚度(累计/平均厚度取各段 edep-sdep 之差)，输出结构化"
        "统计结果，并生成 echarts 柱状图 option(横轴为结论类别，纵轴为累计厚度(m)，"
        "每类储层使用固定颜色)，可直接交给 echarts 组件渲染。文件无该表格时返回空结果。"
    )
    parameters = [
        {
            "name": "gdsx_path",
            "type": "string",
            "description": "GDSX 数据文件路径（必填，需为实际存在的 .gdsx 文件）",
            "required": True,
        },
        {
            "name": "output_dir",
            "type": "string",
            "description": "结果输出目录(可选)，默认使用项目内 gdsx_parser/outputs",
            "required": False,
        },
        {
            "name": "need_chart",
            "type": "bool",
            "description": "是否生成 echarts 柱状图 option(可选)，默认 true 绘图",
            "required": False,
        },
        {
            "name": "save_files",
            "type": "bool",
            "description": "是否将结果/option JSON 落盘(可选，默认 false)。仅本地测试需要，部署后不用",
            "required": False,
        },
    ]

    def run(self, p: str, **kwargs) -> str:
        params = self._verify_json_format_args(p or "{}")
        gdsx_path = params.get("gdsx_path", "")
        output_dir = params.get("output_dir") or None
        need_chart = bool(params.get("need_chart", True))
        save_files = bool(params.get("save_files", False))

        result = run_ogresult_distribution(
            gdsx_path=gdsx_path,
            output_dir=output_dir,
            need_chart=need_chart,
            save_files=save_files,
        )
        return json.dumps(result, ensure_ascii=False, default=str)


class GdsxOgResultCrossplotTool(AgentScopeJsonTool):
    """输入 GDSX 文件路径，结合油气结论表与 POR/PERM/SW 曲线，绘制两两交会散点图。"""
    name = "gdsx_ogresult_crossplot"
    description = (
        "GDSX测井数据油气结论交会散点图。输入 GDSX 数据文件路径，读取油气结论表格"
        "(键名兼容 OGRESULT/油气结论)与孔隙度(POR)、渗透率(PERM)、含水饱和度(SW)曲线，"
        "仅取落在油气结论表各储层深度区间内的点，每点按其所在储层结论的固定颜色着色"
        "(颜色映射与「GDSX油气结论储层分布统计」一致)，按结论分组输出三张散点图："
        "①孔隙度-渗透率(PERM 纵轴取对数) → ②孔隙度-含水饱和度 → ③渗透率(横轴取对数)"
        "-含水饱和度。返回 echarts_option 字典(键 por_perm/por_sw/perm_sw)及各自落盘路径，"
        "缺 POR/PERM/SW 任一切线则报错。"
    )
    parameters = [
        {
            "name": "gdsx_path",
            "type": "string",
            "description": "GDSX 数据文件路径（必填，需为实际存在的 .gdsx 文件）",
            "required": True,
        },
        {
            "name": "output_dir",
            "type": "string",
            "description": "结果输出目录(可选)，默认使用项目内 gdsx_parser/outputs",
            "required": False,
        },
        {
            "name": "need_chart",
            "type": "bool",
            "description": "是否生成 echarts 散点图 option(可选)，默认 true 绘图",
            "required": False,
        },
        {
            "name": "save_files",
            "type": "bool",
            "description": "是否将各 option JSON 落盘(可选，默认 false)。仅本地测试需要，部署后不用",
            "required": False,
        },
    ]

    def run(self, p: str, **kwargs) -> str:
        params = self._verify_json_format_args(p or "{}")
        gdsx_path = params.get("gdsx_path", "")
        output_dir = params.get("output_dir") or None
        need_chart = bool(params.get("need_chart", True))
        save_files = bool(params.get("save_files", False))

        result = run_ogresult_crossplot(
            gdsx_path=gdsx_path,
            output_dir=output_dir,
            need_chart=need_chart,
            save_files=save_files,
        )
        return json.dumps(result, ensure_ascii=False, default=str)
