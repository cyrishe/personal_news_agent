# CC 普通聊天生活服务能力

## 边界

生活服务位于 `personal_news_agent/everyday/`，不依赖新闻存储、搜索、Skill 注册表或聊天路由。
它只暴露窄的只读工具契约，CC Runtime 在普通聊天中按配置动态注册；新闻研究、定时任务和复杂新闻 Skill
默认不会看到这些工具。

当前工具：

- `weather_lookup`：全球地点的实况与最多四天天气预报，无需申请 Key。
- `route_plan`：国内步行、驾车和公共交通路线。
- `train_schedule`：按出发站、到达站和日期查询车次。
- `flight_status`：按航班号和日期查询航班动态。

所有第三方返回值都标记为不可信实时数据。工具只做查询，不订票、不支付、不保存用户位置；单次请求默认
10 秒超时且不自动重试，避免对计费接口重复扣费。工具错误会返回结构化失败结果，CC 可转用已授权的外部搜索。

## Provider

天气默认使用 [Open-Meteo](https://open-meteo.com/en/docs)，非商用场景无需注册或 API Key。地点解析使用
其 GeoNames 数据，返回结果保留来源标识。高德 Key 为空时即可直接使用：

```dotenv
PNA_OPEN_METEO_WEATHER_ENDPOINT=https://api.open-meteo.com/v1/forecast
PNA_OPEN_METEO_GEOCODING_ENDPOINT=https://geocoding-api.open-meteo.com/v1/search
```

地图路线仍使用[高德 Web 服务 API](https://developer.amap.com/api/webservice/summary)。配置高德 Web 服务
Key 后，启用国内步行、公交和驾车路线，并由高德天气替代 Open-Meteo：

```dotenv
PNA_AMAP_WEB_KEY=
```

火车和航班使用聚合数据的[火车订票查询](https://www.juhe.cn/docs/api/id/817)与
[全球航班动态](https://www.juhe.cn/docs/download/pdf/498)。需要在聚合数据账户中分别开通对应产品；两项能力独立配置：

```dotenv
PNA_JUHE_TRAIN_KEY=
PNA_JUHE_FLIGHT_KEY=
```

高德或聚合数据未配置 Key 时，对应路线、火车、航班工具不会注册，不影响 CC 或其他服务启动。用户开启
“联网回答”后，CC 可用内置 WebSearch 对这些场景做轻量检索；搜索结果只作为参考，必须提示临行复核。

## 接线

`EverydayCapabilityService.from_settings()` 只在服务工厂创建一次。`CCRuntimeOrchestrator.build_options()`
收到 `allow_everyday_tools=True` 时，把已配置工具放入独立的 `pna_everyday` MCP server。当前只有
`NewsChatService._general_chat()` 会传入该开关。

火车查询结果必须提示在铁路 12306 复核；航班结果必须提示在承运航司或机场复核；地图耗时、路况和限制
必须提示临行复核。未来替换 Provider 时，只需实现相同的 `EverydayToolSpec`，无需修改聊天分流或新闻 Skill。
