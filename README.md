# 电商图 AI 工作台（Python 本地版）

这是根据《电商图AI工作台_Python重构开发文档.md》和 `0722稳定版本 (2).json` 重构的 Windows 本地 Web 应用。它不依赖 n8n，默认使用本地模拟图片服务方便验收；配置真实的 OpenAI 兼容 `/v1/images/edits` 接口后即可生成真实图片。

## 功能

- stt 手托比例参考图必传；单品只使用 stt 同时锁定外观与比例，系列品还必须上传 cpt，由 cpt 锁定外观、stt 只锁定手托比例。
- 按配置生成 03～13 图片任务，并发生成、单任务失败不影响其他任务。
- 卡片式审核：编辑完整提示词重新生成，或基于当前成品图做局部编辑。
- 历史版本、父版本、版本模式和最终版本确认。
- 新建项目时可无限添加“额外参考图需求”：每条需求可上传多张参考图并填写独立要求，系统会使用“产品图 + 该条参考图 + 需求描述”单独生成一张图。
- 新建页可选“禁止食用水印”：使用内置警示底稿和当前 stt/cpt 生成产品专属英文 `Not Edible` 水印，自动抠成透明图并本地叠加到图03～06右下角；原始摆放图永不覆盖，重出源图或水印后会自动重建派生图。
- 新建页可选“场景拼图 Photoshop 素材包”：分别选择场景模板 Agent 和艺术文字 Agent，配合至少4张已勾选的场景图，自动输出模板原图/透明参考、场景图原素材、艺术文字原图/透明PNG和素材清单 ZIP，供人工 Photoshop 拼图。
- 设置页的“功能图”分页可维护尺寸图、卖点图、结构图等完整模板；图13选择首张功能图模板，新建页底部还可继续追加任意数量的功能图任务，每张均由 GPT-5.5 独立重写。
- SQLite 持久化，图片和 manifest 保存在项目输出目录，支持下载和打包最终图。
- 设置页可修改默认输出/导出目录、API 地址、API Key、模型、图片质量、并发和模拟模式；描述词设计按“功能图 / 摆放图 / 场景图”三个分页维护，设置保存在本地 `.env` 与 `data/runtime_config.json`。
- GPT-5.5 会按三种工作流分批分析：功能图只接收所选完整模板与相关字段，摆放图只接收数量与摆放要求，普通场景图不继承摆放要求，只有图11接收自定义场景。最终提示词自动规范为中文。
- 参考图使用双层约束：描述词保留【产品外观参考图 / 手托比例参考图 / 系列外观参考图 / 额外需求参考图】变量；调用图片接口时再根据实际 multipart 上传顺序生成“输入参考图1、2、3”的角色对应表。单品的【产品外观参考图】动态指向 stt；系列品动态指向 cpt，且 stt 只能控制比例，不能覆盖 cpt 外观。
- 图片卡片只显示和编辑最终画面描述词；stt/cpt 的参考图角色契约属于内部控制信息，只在 GPT 分析和生图 API 请求时自动注入，不再重复显示在每张描述词开头。
- 内置描述词采用工作流式双花括号占位词。设置页模板保留 `{{产品数量}}`、`{{摆放展示要求}}`、`{{自定义使用场景}}`、`{{产品尺寸}}`、`{{产品外观参考图}}`、`{{手托比例参考图}}`、`{{系列外观参考图}}` 等变量，创建或重新分析项目时才替换当前项目的实际值；未知变量会原样保留，避免静默丢失。原工作流 `{{stt}}`、`{{cpt}}` 分别兼容映射到手托比例图和系列外观图。

## Windows 启动

需要 Python 3.12+。

```powershell
cd C:\path\to\ecommerce_image_workbench
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python app.py
```

浏览器打开 http://127.0.0.1:8080 。如果没有配置 API Key，应用会自动进入模拟模式，生成可预览的占位图；这用于测试页面和版本流程，不代表真实生图结果。

## 真实接口配置

编辑 `.env`：

```env
IMAGE_API_BASE_URL=https://example.com
IMAGE_API_KEY=replace_me
IMAGE_MODEL=gpt-image-2
OUTPUT_ROOT=./data/outputs
PROMPT_API_BASE_URL=https://example.com
PROMPT_API_KEY=replace_me
PROMPT_MODEL=gpt-5.5
MOCK_MODE=false
```

`IMAGE_API_BASE_URL` 可以填写带或不带 `/v1` 的根地址，客户端会请求 `{BASE_URL}/v1/images/edits`。当前工作台已按用户提供的 OhMyGPT 网关写入本地 `.env`，图片模型默认 `gpt-image-2`，提示词模型默认 `gpt-5.5`。密钥只在后端读取，不会返回给浏览器或写入日志；`.env` 已被 `.gitignore` 排除。

如果出现 `403 / 1010`，通常是网关的 Cloudflare/IP 访问策略拦截了当前电脑或网络环境，需要在网关侧放行后再重试。

## 输出目录

默认在项目目录下的 `data/outputs/`，也可以在“设置 → 默认输出/导出目录”中修改；新建项目会使用新目录，已有项目不会自动移动：

```text
data/
├── app.db
└── outputs/
    └── {project_id}_{safe_product_name}/
        ├── input/
        ├── extra_requests/
        ├── tasks/
        ├── postprocess/
        │   ├── not_edible/        # 透明水印和03～06的另存水印版
        │   └── collage/           # 场景拼图素材目录与ZIP
        └── project_manifest.json
    └── {product_name}_{YYYY-MM-DD}/
        └── 所有已确认或最新成功的最终图片
```

点击“导出最终图”时，会直接在目标输出目录创建“产品名+日期”文件夹，同时下载一个图片直接位于压缩包根目录的 ZIP；启用禁止食用流程后，图03～06导出匹配当前源版本的水印成品，源图继续保留在任务目录；场景拼图素材包完成后也会随最终导出一起保存。重复导出会增加 `_02`、`_03` 后缀避免覆盖。备份或迁移时应关闭应用后复制整个项目，并同步更新 `.env` 的 `OUTPUT_ROOT` 以及数据库中的项目输出目录。

## 工作流迁移说明

见 [WORKFLOW_MAPPING.md](WORKFLOW_MAPPING.md)。其中保留了图08断线和图13白底图/尺寸图冲突，具体任务由 [config/task_definitions.py](config/task_definitions.py) 配置驱动。

## 常见问题

- 页面打不开：确认终端仍在运行，并访问 `127.0.0.1:8080`。
- 图片一直等待：先用模拟模式验证；真实 API 需要支持 multipart `image[]` 和返回 `data[].b64_json`。
- 真实接口报 401/403：检查 `.env` 中的 Key 和 Base URL，不要把 Key 写进前端。
- 上传失败：当前只接受 jpg/jpeg/png/webp，单文件默认上限 20MB。
