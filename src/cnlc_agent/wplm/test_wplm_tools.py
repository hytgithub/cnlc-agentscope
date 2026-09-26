"""
测井预测大模型工具链测试脚本
依次运行 DataPreprocessingTool、WellLogPredictionModelTool、PostProcessTool 三个工具并输出日志。

运行方式（在项目根目录下）:
    uv run python -m cnlc_agent.wplm.test_wplm_tools
或:

"""
import json
import logging
import os
import sys

from cnlc_agent.wplm.tools import DataPreprocessingTool, WellLogPredictionModelTool, PostProcessTool,GdsxCompletenessTool,GdsxExpansionTool,GdsxCrossplotTool


def setup_logger(name: str) -> logging.Logger:
    """配置控制台日志输出。"""
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        formatter = logging.Formatter(
            '%(asctime)s | %(levelname)s | %(name)s | %(message)s',
            datefmt='%H:%M:%S',
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger


log = setup_logger('wplm.test')


def run_data_preprocessing():
    """测试 DataPreprocessingTool（数据预处理工具）。"""
    log.info('===== 开始运行 DataPreprocessingTool =====')
    tool = DataPreprocessingTool()
    params = {
        'raw_gdsx_file_path': 'input_well_A.gdsx',
        'operations': '去噪、标准化、缺失值填充、异常值剔除',
        'wplm_preprocessed_gdsx_file_path': 'wplm_preprocessed.gdsx',
    }
    log.info('输入参数: %s', json.dumps(params, ensure_ascii=False))
    result = tool.run(params)
    log.info('返回结果: %s', result)
    log.info('===== DataPreprocessingTool 运行结束 =====\n')
    return json.loads(result)


def run_welllog_prediction(log_req_json: str):
    """测试 WellLogPredictionModelTool（测井预测大模型推理工具）。"""
    log.info('===== 开始运行 WellLogPredictionModelTool =====')
    tool = WellLogPredictionModelTool()
    params = {
        'wellName': '测试井',
        'serviceId': '71c2d18bda894157a5558163a0564533',
        'taskConfig': {
            'CLS': ['DZFC', 'CCHF', 'JSJL'],
            'NUM': ['POR', 'SW', 'PERM', 'SH', 'SAND', 'LIME', 'DOLO', 'CARB', 'ANHY'],
        },
        'batchSize': 1024,
        'createPeople': '智能体平台',
        # 由 DataPreprocessingTool 返回的 logReqJson 提供数据体
        'logReqJson': json.loads(log_req_json) if log_req_json else {},
    }
    log.info('输入参数(不含 logReqJson 明细): %s',
             json.dumps({k: v for k, v in params.items() if k != 'logReqJson'}, ensure_ascii=False))
    result = tool.run(params)
    log.info('返回结果: %s', result)
    log.info('===== WellLogPredictionModelTool 运行结束 =====\n')
    return json.loads(result)


def run_post_process(predictions):
    """测试 PostProcessTool（测井预测大模型后处理工具）。"""
    log.info('===== 开始运行 PostProcessTool =====')
    tool = PostProcessTool()
    params = {
        'predictions': predictions,
        'wplm_postprocessed_gdsx_file_path': 'wplm_postprocessed.gdsx',
    }
    log.info('输入参数: %s', json.dumps(params, ensure_ascii=False))
    result = tool.run(params)
    log.info('返回结果: %s', result)
    log.info('===== PostProcessTool 运行结束 =====\n')
    return json.loads(result)
def runGdsxCompletenessTool():
    """测试 GdsxCompletenessTool（数据预处理工具）。"""
    log.info('===== 开始运行 GdsxCompletenessTool =====')
    tool = GdsxCompletenessTool()
    params = {
        'gdsx_path': 'D:/program/cnlc/gdsx_parser/gdsx/米脂3_ECLIPS-5700_常规大组合_1872-2763_20251128_完井.gdsx',
        'need_chart':  True,
        'curves': ["AC","CNL", "DEN", "PE", "RT", "RXO", "SSR"],
    }
    log.info('输入参数: %s', json.dumps(params, ensure_ascii=False))
    result = tool.run(params)
    log.info('返回结果: %s', result)
    log.info('===== DataPreprocessingTool 运行结束 =====\n')
    return json.loads(result)


def main():
    log.info('########## 测井预测大模型工具链测试开始 ##########\n')

    # # 1. 数据预处理
    # preprocessed = run_data_preprocessing()
    #
    # # 2. 推理（携带预处理返回的 logReqJson）
    # log_req_json = preprocessed.get('logReqJson', '')
    # inference = run_welllog_prediction(log_req_json)
    #
    # # 3. 后处理（携带推理返回的完整结果）
    # # 注意: WellLogPredictionModelTool 返回的是推理接口原始响应
    # # (msg/code/data 结构)，此处直接将其作为 predictions 传入 PostProcessTool。
    # run_post_process(inference)
    runGdsxCompletenessTool()
    log.info('########## 测井预测大模型工具链测试结束 ##########')


if __name__ == '__main__':
    main()