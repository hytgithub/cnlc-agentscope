"""
文件解编相关工具
"""
import json
import os

import requests

from cnlc_agent.wplm.agentscope_tool import AgentScopeJsonTool

# DataPreProcess 服务地址，可通过环境变量覆盖
DPP_BASE_URL = os.environ.get('DPP_BASE_URL', 'http://localhost:8686')
DPP_TIMEOUT_SECONDS = int(os.environ.get('DPP_TIMEOUT_SECONDS', '300'))


class FileDecodeTool(AgentScopeJsonTool):
    """测井文件解编工具 (陈歆)"""
    name = "file_decode"
    description = '根据用户输入的原始测井数据文件路径，调用 DataPreProcess 服务进行文件解编（格式转换），输出 GDSX 数据文件路径。依次调用文件上传、格式转换、文件下载三个接口。'
    parameters = [{
        'name': 'raw_file_path',
        'type': 'string',
        'description': '测井原始数据文件路径（如：.gds、.gdx、.lis、.dlis、.txt 等格式）',
        'required': True,
    }, {
        'name': 'output_ext',
        'type': 'string',
        'description': '输出编码格式，如 GDSX、GDS、GDX、LDF 等，默认为 GDSX',
        'required': False,
    }, {
        'name': 'curvelist',
        'type': 'array',
        'description': '需要解编的曲线名称列表，为空则解编全部曲线',
        'required': False,
    }
    ]

    def run(self, params: str, **kwargs) -> str:
        """执行测井文件解编（格式转换）。

        依次调用 DataPreProcess 服务的三个接口：
          1. POST /api/file/upload   — 上传原始文件
          2. POST /api/data/convert   — 格式转换（encoder 默认 GDSX）
          3. POST /api/file/download  — 下载转换后的文件到本地

        参数:
            params (str): JSON 字符串，包含:
                - raw_file_path (str): 原始测井数据文件路径
                - output_ext (str, 可选): 输出编码格式，默认 GDSX
                - curvelist (array, 可选): 需要解编的曲线名称列表

        返回:
            JSON 字符串，字段含义:
                - success (bool): 是否处理成功
                - dataSource (str): 原始文件路径
                - output_format (str): 输出编码格式
                - output_file_path (str): 转换后文件的本地存储路径
                - message (str): 处理结果描述
        """
        params_dict = json.loads(params) if isinstance(params, str) else params
        raw_file_path = params_dict.get('raw_file_path', '')
        output_ext = (params_dict.get('output_ext') or 'GDSX').upper()
        curvelist = params_dict.get('curvelist', [])

        # 校验本地文件是否存在
        if not raw_file_path or not os.path.exists(raw_file_path):
            return json.dumps({
                'success': False,
                'message': f'文件不存在: {raw_file_path}',
            }, ensure_ascii=False)

        local_dir = os.path.dirname(raw_file_path) or '.'
        base_name = os.path.splitext(os.path.basename(raw_file_path))[0]

        # ---- 第1步：文件上传 /api/file/upload ----
        try:
            with open(raw_file_path, 'rb') as f:
                upload_resp = requests.post(
                    f'{DPP_BASE_URL}/api/file/upload',
                    files={'file': (os.path.basename(raw_file_path), f)},
                    timeout=DPP_TIMEOUT_SECONDS,
                )
            upload_resp.raise_for_status()
        except requests.RequestException as e:
            return json.dumps({
                'success': False,
                'message': f'文件上传失败: {e}',
            }, ensure_ascii=False)

        upload_data = upload_resp.json().get('data', {})
        server_path = upload_data.get('filePath', '')
        if not server_path:
            return json.dumps({
                'success': False,
                'message': '文件上传失败：未获取到服务器文件路径',
            }, ensure_ascii=False)

        # ---- 第2步：格式转换 /api/data/convert ----
        convert_body = {
            'srcfile': server_path,
            'encoder': output_ext,
        }
        if curvelist:
            convert_body['curvelist'] = curvelist

        try:
            convert_resp = requests.post(
                f'{DPP_BASE_URL}/api/data/convert',
                json=convert_body,
                timeout=DPP_TIMEOUT_SECONDS,
            )
            convert_resp.raise_for_status()
        except requests.RequestException as e:
            return json.dumps({
                'success': False,
                'message': f'格式转换失败: {e}',
            }, ensure_ascii=False)

        convert_json = convert_resp.json()
        convert_code = convert_json.get('code', -1)
        if convert_code != 200:
            return json.dumps({
                'success': False,
                'message': f'格式转换失败: {convert_json.get("msg", f"code={convert_code}")}',
            }, ensure_ascii=False)

        convert_result = convert_json.get('data', {})
        converted_server_path = convert_result.get('result', '')
        if not converted_server_path:
            return json.dumps({
                'success': False,
                'message': '格式转换失败：未获取到转换后的服务器文件路径',
            }, ensure_ascii=False)

        # ---- 第3步：文件下载 /api/file/download ----
        try:
            download_resp = requests.post(
                f'{DPP_BASE_URL}/api/file/download',
                json={'filePath': converted_server_path},
                timeout=DPP_TIMEOUT_SECONDS,
            )
            download_resp.raise_for_status()
        except requests.RequestException as e:
            return json.dumps({
                'success': False,
                'message': f'文件下载失败: {e}',
            }, ensure_ascii=False)

        # 保存到本地：{原文件名}.{输出编码格式小写}
        output_file_path = os.path.join(local_dir, f'{base_name}.{output_ext.lower()}')
        if os.path.abspath(output_file_path) == os.path.abspath(raw_file_path):
            # 输入已经是目标格式时，不允许下载结果覆盖用户提供的原件。
            output_file_path = os.path.join(local_dir, f'{base_name}_converted.{output_ext.lower()}')
        with open(output_file_path, 'wb') as df:
            df.write(download_resp.content)

        return json.dumps({
            'success': True,
            'dataSource': raw_file_path,
            'output_format': output_ext,
            'output_file_path': output_file_path,
            'message': f'文件解编完成，输出文件: {output_file_path}',
        }, ensure_ascii=False)


if __name__ == '__main__':
    """测试代码：验证文件解编工具的完整流程（上传→转换→下载）"""

    # 测试文件路径（基于当前脚本所在目录解析，避免受工作目录影响）
    _script_dir = os.path.dirname(os.path.abspath(__file__))
    test_file = os.path.join(_script_dir, '工程测井20260610_144745_主测_relog_20260722_132854.ldf')

    print(f'DPP_BASE_URL = {DPP_BASE_URL}')
    print(f'测试文件 = {test_file}')

    # --- 测试 FileDecodeTool ---
    tool = FileDecodeTool()
    params = json.dumps({
        'raw_file_path': test_file,
        'output_ext': 'GDSX',
        'curvelist': ['GR', 'AC'],
    }, ensure_ascii=False)

    print('\n===== 开始测试 FileDecodeTool =====')
    result = tool.run(json.loads(params))
    print('返回结果:')
    print(json.dumps(json.loads(result), ensure_ascii=False, indent=2))

    result_dict = json.loads(result)
    if result_dict.get('success'):
        print(f'\noutput_format     = {result_dict["output_format"]}')
        print(f'output_file_path  = {result_dict["output_file_path"]}')
    else:
        print(f'\n处理失败: {result_dict.get("message", "未知错误")}')
