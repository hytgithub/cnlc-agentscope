1.服务筛选
http://10.242.0.164:8100/center-management/center/ServiceInfo/get-page/v1
Post
{"name":"","pageNum":1,"pageSize":100,"status":"运行中"}
{
    "success": true,
    "msg": "分页查询成功。",
    "obj": {
        "itemTotal": 1,
        "startIndex": 0,
        "items": [
            {
                "id": "0dd3a0f2c7fd4a5380bbab3c6dec1742",
                "server": "集团服务器05",
                "status": "运行中",
                "type": "陇东大模型复现-V2测试服务",
                "modelId": "170678805cbf4c6b8eed6ef2d9f73c19",
                "url": null,
                "createPeople": "刘育博",
                "createDate": "2026-08-05 08:49:49",
                "createDateStart": null,
                "createDateEnd": null,
                "modelName": "陇东大模型复现-V2-lyb",
                "modelVersion": "2.0",
                "modelPath": "Output/4337f7_8dot8k_v1_sft_16_v_encode_1784284339501-country-v1-train-sft-170678805cbf4c6b8eed6ef2d9f73c19/checkpoint-21500",
                "pageNum": 0,
                "pageSize": 0,
                "isDel": null
            }
        ],
        "pageSize": 100,
        "pageCurrent": 1,
        "pageCount": 0
    },
    "number": "1",
    "args": null
}

2.单井编码推理
http://10.242.0.164:8100/center-management/center/InferenceLog/encodingInferenceStart/v1
Post
{
    "encodingUrl": "",
    "serviceId": "71c2d18bda894157a5558163a0564533",传入的为上一次查询出来的列表中id
    "taskConfig": {
        "CLS": [
            "DZFC",
            "CCHF",
            "JSJL"
        ],
        "NUM": [
            "POR",
            "SW",
            "PERM",
            "SH",
            "SAND",
            "LIME",
            "DOLO",
            "CARB",
            "ANHY"
        ]
    },
    "batchSize": 1024,
    "createPeople": "zyg",
    "wellName": "测试井",
    "logReqJson": {
        "area": "huan",
        "logname": "悦212_常规大组合_20210419_1111-2171",
        "qxm": [
            "AC",
            "AT20",
            "AT90",
            "CAL",
            "CNL",
            "DEN",
            "GR",
            "PE",
            "RT",
            "RXO",
            "SP",
            "UPOSX",
            "UPOSY",
            "UPOSZ",
            "DEPTH"
        ],
略
    }
}
返回{
    "msg": "单井推理发起成功",
    "code": 200,
    "data": "93e5100d47cb401182dfeab69a12ee10"
}



3.单井推理结果查询
http://10.242.0.164:8100/center-management/center/InferenceLog/inferenceFetch/v1?server=集团服务器04&taskName=bdbd2c89dae14fb2972cbd8193c5b073
taskname为发起接口的返回data，server为接口1返回的服务器名称server
Get
推理中返回
{
    "msg": "单井推理获取成功",
"code": 200,
“data”: {
  "code": 200,
  "msg": "推理状态获取成功",
  "data": {
    "status": "PROCESSING",
    "totalNum": 8338,
    "processedNum": 1024,
    "startTime": "2026-05-08\n...\n6426+08:00",
    "endTime": null
  }
}
}


推理完成返回：见文件
4.数据预处理接口

# GDSX 数据处理接口文档
调用ip 10.242.0.164:8686
允许访问ip：10.242.230.8，10.242.0.107
**接口路由**: `/api/data/processForGDSX`  
**更新时间**: 2026-08-13

---

## 1. 接口概述

该接口对上传的 GDSX 测井数据文件执行以下操作：
1. **标准预处理**：曲线命名标准化、曲线单位标准化、重采样、井坐标生成等（按 `index` 升序依次执行）
2. **曲线数据采样读取**：根据请求的 `curves` 列表，按重采样间隔逐深度点读取曲线数值
3. **返回标准 JSON 结构**：包含曲线数值数组、深度数组、曲线顺序列表等

**仅支持单文件处理**：若上传多个 files，仅处理第一个文件。

---

## 2. 请求方式

| 项目 | 值 |
|---|---|
| Method | `POST` |
| Content-Type | `multipart/form-data` |
| 同步调用 | `POST /api/data/processForGDSX` |建议采用同步
| 异步调用 | `POST /api/data/processForGDSX/async` |
| 异步状态查询 | `GET /api/task?taskId=<taskId>` |

---

## 3. 请求参数 (form-data)

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `para` | text (application/json) | 是 | 处理参数 JSON，详见 3.1 |
| `files` | file | 是 | 待处理的数据文件（.gdsx / .gdx 等） |

### 3.1 para 参数结构

```json
{  
    "curves": [
        "AC",
        "CAL",
        "CNL",
        "DEN",
        "GR",
        "PE",
        "RT",
        "RXO",
        "SP",
        "UPOSX",
        "UPOSY",
        "UPOSZ"
    ],
    "curveNameStandard": {
        "enable": true,
        "index": 1,
        "data": {
            "RT": "AT20",
            "RXO": "AT90"
        }
    },
    "curveUnitStandard": {
        "enable": true,
        "index": 2,
        "data": {
            "GR": "API",
            "CGR": "API",
            "PE": "b/e",
            "SP": "mV",
            "AC": "μs/m",
            "DEN": "g/cm³",
            "CNL": "%",
            "RXO": "OHMM",
            "RT": "OHMM",
            "AZIM": "DEG",
            "DEVI": "DEG"
        }
    },
    "wellCoordinateGenerate": {
        "enable": true,
        "index": 3
    },
    "resample": {
        "enable": true,
        "index": 4,
        "data": {
            "defaultInterval": 0.1
        }
    }
}
```

### 3.2 para 字段说明

#### `curves` (string[], **必填**)
需要从结果中读取的曲线名称列表。
- **必须包含 `UPOSX`、`UPOSY`、`UPOSZ`**：用于确定深度范围和井眼轨迹
- 支持的常规曲线：`AC`、`CAL`、`CNL`、`DEN`、`GR`、`PE`、`RT`、`RXO`、`SP` 等

#### 标准处理模块结构（所有模块共享相同格式）

| 字段 | 类型 | 说明 |
|---|---|---|
| `enable` | bool | 是否启用该步骤 |
| `index` | int | 执行顺序，按从小到大依次执行 |
| `data` | object / bool | 该模块的配置参数 |

#### `curveNameStandard` — 曲线命名标准化
`data` 为 `{标准名: "别名1,别名2,..."}` 映射。处理时将文件中的别名曲线改名为标准名。

#### `geologyLayerStandard` — 地质分层名称标准化（可选）
`data` 为 `{标准层位: "别名1,别名2,..."}` 映射。

#### `interpretResultStandard` — 解释结论标准化（可选）
`data` 为 `{标准结论: "别名1,别名2,..."}` 映射。

#### `curveUnitStandard` — 曲线单位统一
`data` 为 `{曲线名: "标准单位"}` 映射。

#### `resample` — 曲线重采样
| 子字段 | 类型 | 说明 |
|---|---|---|
| `data.defaultInterval` | double | 全局默认重采样间隔（米），如 `0.125` |
| `data.specialCurveInterval` | object | **可选**，单曲线特殊间隔，如 `{"AC":0.1, "GR":0.2}` |

⚠️ `defaultInterval` 同时也是读取曲线数据时的深度步长。

#### `wellCoordinateGenerate` — 井下坐标生成
`data` 直接传 `true` 表示启用。

---

## 4. 响应格式

### 4.1 成功响应 (HTTP 200)

```json
{
    "msg": "success",
    "code": 200,
    "data": {
        "AC": [220.50, 220.62, 220.75],
        "GR": [85.20, 86.10, 84.50],
        "UPOSX": [100.50, 100.51, 100.52],
        "UPOSY": [200.30, 200.31, 200.32],
        "UPOSZ": [1500.00, 1500.12, 1500.25],
        "DEPTH": [1500.00, 1500.12, 1500.25],
        "qxm": ["AC", "GR", "UPOSX", "UPOSY", "UPOSZ", "DEPTH"],
        "area": "huan",
        "logname": "well_A",
        "moduleResults": {
            "curveNameStandard": true,
            "curveUnitStandard": true,
            "resample": true,
            "wellCoordinateGenerate": true
        }
    }
}
```

### 4.2 响应 data 字段说明

| 字段 | 类型 | 说明 |
|---|---|---|
| `<曲线名>` | number[] | 每条请求的曲线对应一个数值数组，长度 = 深度点数。保留 2 位小数 |
| `DEPTH` | number[] | 深度数组，与曲线数组一一对应。步长 = `resample.data.defaultInterval` |
| `qxm` | string[] | 曲线名称顺序列表（含 `DEPTH`），与数组顺序对应 |
| `area` | string | 区域标识，固定为 `"huan"` |
| `logname` | string | 日志名称 = 上传文件名（去除后缀） |
| `moduleResults` | object | 各标准处理模块的执行结果（true=成功，false=失败） |

### 4.3 失败响应

**缺少必需曲线 (HTTP 400)**
```json
{
    "msg": "Parameter error, curves parameter must contain UPOSX, UPOSY, UPOSZ curves",
    "code": 400,
    "data": {}
}
```

**未上传文件 (HTTP 400)**
```json
{
    "msg": "参数错误：未上传数据文件（form-data 文件字段）",
    "code": 400,
    "data": {}
}
```

**处理失败 (HTTP 500)**
```json
{
    "msg": "[curveNameStandard] processing failed: Failed to get curve list",
    "code": 500,
    "data": {}
}
```

---

## 5. 异步调用流程

> **统一结构约定**：同步和异步任务查询的返回结构完全一致 —— 顶层固定包含 `msg`、`code`、`data` 三个字段。
> 异步任务查询特有的字段（`taskId`、`status`、`progress`、`progressMsg`、`error`）作为顶层字段，
> 与 `msg`、`code`、`data` **平级**，不放入 `data` 内。
> `data` 只存放业务数据，与同步调用的 `data` 结构完全一致。

### 5.1 提交异步任务
```
POST /api/data/processForGDSX/async
Content-Type: multipart/form-data
（body 与同步调用相同）
```

**响应 (HTTP 202)**:
```json
{
    "msg": "异步任务已提交",
    "code": 202,
    "data": {
        "taskId": "task_20260813_143025_5",
        "statusQuery": "/api/task?taskId=task_20260813_143025_5"
    }
}
```

### 5.2 查询任务状态
```
GET /api/task?taskId=task_20260813_143025_5
```

**处理中 (running / pending)** — HTTP 202：
```json
{
    "msg": "任务处理中",
    "code": 202,
    "data": {},
    "taskId": "task_20260813_143025_5",
    "status": "running",
    "progress": 55,
    "progressMsg": "读取曲线数据，共8841个深度点"
}
```

**处理完成 (completed)** — HTTP 200：
`data` 中的业务字段与同步调用**完全一致**，额外字段 `taskId / status / progress / progressMsg` 平级放在顶层：
```json
{
    "msg": "success",
    "code": 200,
    "data": {
        "AC": [220.50, 220.62, 220.75],
        "GR": [85.20, 86.10, 84.50],
        "UPOSX": [100.50, 100.51, 100.52],
        "UPOSY": [200.30, 200.31, 200.32],
        "UPOSZ": [1500.00, 1500.12, 1500.25],
        "DEPTH": [1500.00, 1500.12, 1500.25],
        "qxm": ["AC", "GR", "UPOSX", "UPOSY", "UPOSZ", "DEPTH"],
        "area": "huan",
        "logname": "well_A",
        "moduleResults": {
            "curveNameStandard": true,
            "curveUnitStandard": true,
            "resample": true,
            "wellCoordinateGenerate": true
        }
    },
    "taskId": "task_20260813_143025_5",
    "status": "completed",
    "progress": 100,
    "progressMsg": "处理完成"
}
```

**处理失败 (failed)** — HTTP 500：
```json
{
    "msg": "[curveNameStandard] processing failed: Failed to get curve list",
    "code": 500,
    "data": {},
    "taskId": "task_20260813_143025_5",
    "status": "failed",
    "progress": 20,
    "progressMsg": "处理文件: well_A.gdsx",
    "error": "[curveNameStandard] processing failed: Failed to get curve list"
}
```

**任务不存在** — HTTP 404：
```json
{
    "msg": "任务不存在",
    "code": 404,
    "data": {}
}
```

---

## 6. 进度说明 (0% ~ 100%)

| 进度 | 阶段 | 进度消息示例 |
|---|---|---|
| 0% | 启动 | 开始处理：解析请求参数 |
| 10% | 参数解析完成 | 参数解析完成：12条曲线 |
| 20% | 开始流式处理 | 处理文件: well_A.gdsx |
| 20%~50% | 标准模块处理 | （由 `EDStreamDataProcess` 内部执行） |
| 50% | 开始曲线读取 | 读取曲线数据，共8841个深度点 |
| 50%~95% | 曲线逐条读取 | 读取曲线进度: 3/12 |
| 95%~100% | 收尾 | 处理完成 |

---

