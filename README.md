# 沿海潮位卫星短报文采集与小时基准核验

沿海潮位站经卫星短报文回传分钟潮位，网关侧会留下**拆段、乱序、重投**的 NDJSON
记录。本服务（FastAPI + SQLite）按 `站号 + 设备启动号 + 报文号` 归组报文段，
收齐后按段号拼接字节、校验整报 SHA-256，解析其中的 UTC 分钟潮位 CSV 并发布；
再接收海事测绘部门提供的**站点小时基准潮位**，把已确认观测按 `站号 + 分钟`
与基准配对核验，合格数据供监测库下载。

## 快速启动

### Docker Compose（推荐）

```bash
docker compose up --build
# 打开 http://localhost:8000
```

数据库持久化在命名卷 `tide-data`（容器内 `/data/tide.db`），重启不丢状态。
旧库首次启动会自动补建 `baselines` 表，已有观测无需重传即可参与核验。

### 本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload          # 默认库文件 ./data/tide.db，可用 DB_PATH 覆盖
```

### 运行测试

```bash
pip install -r requirements-dev.txt
pytest
```

## 使用流程

1. 浏览器打开 `http://localhost:8000`，在「1. 上传」选择 NDJSON 文件；
2. 「2. 报文状态」列出每个报文的已收/总段数、**缺段清单**、状态与**待核原因**；
3. 「3. 上传站点小时基准」填入站号与 UTC 小时，选择分钟基准 CSV 上传，
   该站该小时的旧基准被**整小时替换**；下方可展开查看当前各小时基准版本；
4. 「4. 基准核验结果」按站号/小时筛选，逐分钟显示观测值、基准值、差值、
   状态（合格 / 缺基准 / 超差）与**原报文行**；可分别下载：
   - **核验 CSV**（`/api/verification.csv`）：只含差值绝对值 ≤ 0.20 m 的合格观测；
   - **异常明细 CSV**（`/api/verification/exceptions.csv`）：缺基准与超差明细；
5. 「5. 原始已确认观测」与改造前一样可预览、按站号下载 CSV；
6. 待核报文处理完后，点「复位重采」清除该报文，等待岸台重新上送。

命令行等价操作：

```bash
curl -F "file=@samples/sample_upload.ndjson" http://localhost:8000/api/upload
curl -F "station_id=101" -F "hour=2026-11-14T22:00:00Z" \
     -F "file=@samples/sample_baseline_101_2026-11-14T22.csv" \
     http://localhost:8000/api/baselines
curl http://localhost:8000/api/verification
curl -OJ "http://localhost:8000/api/verification.csv?station_id=101"
curl -OJ "http://localhost:8000/api/observations.csv?station_id=101"
```

## NDJSON 记录格式

每行一个 JSON 对象：

| 字段 | 类型 | 说明 |
|---|---|---|
| `station_id` | int | 站号 |
| `boot_id` | int | 设备启动号 |
| `msg_id` | int | 报文号 |
| `seg_no` | int | 段号，1..seg_total |
| `seg_total` | int | 总段数（≥1） |
| `payload_b64` | string | 本段正文的 Base64 |
| `sha256` | string | **整报**（全部段按段号拼接）的 SHA-256 十六进制 |

整报正文为 UTF-8 的分钟潮位 CSV：每行 `YYYY-MM-DDTHH:MM:00Z,<潮位米>`，
允许空行与 `#` 注释行（样例首行即注释）。观测按 `(站号, 分钟)` 唯一。

## 基准 CSV 格式

上传时表单/查询参数指定 `station_id` 与 `hour`（UTC 整点），文件内容为：

```
# 可选注释
time,level_m                 # 可选英文表头
2026-11-14T22:00:00Z,1.96    # 完整 UTC 分钟
22:01,1.97                   # 也可只写 HH:MM（必须在指定小时内）
```

规则：

- 每行 `分钟,基准潮位(m)`；分钟必须是整分钟且属于指定小时；
- **每分钟只能有一个数值**，重复分钟、非数值、`nan`/`inf`、缺列、非 UTF-8
  等任一错误都会使整次上传被拒绝（HTTP 400），**旧基准保持不变**；
- 上传成功即在同一事务里删除该站该小时的旧基准并写入新行；
  数据库写入失败会回滚（HTTP 500），同样保留旧版；
- 空行、`#` 注释行与首行表头会被忽略；空文件视为错误。

## 核验规则

- 以**已提交观测**为准，按 `(站号, 分钟)` 与当前基准配对；
- `|观测值 − 基准值| ≤ 0.20 m` → 合格，进入核验 CSV；
- 无对应基准 → `缺基准`：列出分钟、观测值、原报文行；
- 有基准但超差 → `超差`：列出观测值、基准值、差值与原报文行；
- 核验结果不另存快照，每次都从当前观测与基准派生，因此：
  - 补段导致报文发布、迟到冲突导致观测撤下、复位后重新采集，
    核验结果与核验 CSV 都会**自动随已提交观测变化**；
  - 基准可先于或晚于观测上传，旧库已有观测无需重传；
  - 原始观测与 CSV 下载行为保持不变。

## 一致性与并发语义

- **重投幂等**：完全相同的段只入库一次；观测按 `(站号, 分钟)` 去重，
  重复上传同一文件不会产生重复数据。
- **乱序**：段按归组键随意到达，收齐即组装。
- **待核（pending_review）**：同一报文出现以下任一情况即转待核，并**立即撤下
  该报文已发布的观测**——段内容冲突（同段号不同正文）、总段数冲突、整报摘要
  冲突、段号越界、组装后摘要与登记摘要不符、CSV 解析失败。待核报文后续到达
  的段一律忽略，复位后方可重新采集。
- **基准原子替换**：`DELETE` 当前小时 + `INSERT` 新行在同一个
  `BEGIN IMMEDIATE` 事务内完成，失败整体回滚。
- **并发**：上传、基准替换在进程内写锁 + 单事务内串行落库；核验 JSON/CSV 在
  同一把锁与同一个 SQLite 读事务内取数，因此**每个响应各自对应一个完整且已提交
  的状态**，不会把某次发布的半批观测或新旧两版基准混在同一个响应/CSV 里。
  WAL + `busy_timeout` 保证多进程下不错乱。
- **重启**：全部状态在 SQLite 中，观测只在报文校验通过后才写入，因此重启后
  查询天然只显示完整报文的观测，重投依旧幂等。

## 样例文件

- `samples/sample_upload.ndjson` —— 乱序 + 重投的正常样例：报文 1001（4 段）、
  1002（3 段）可发布；1003 缺第 2 段，网页「缺段」列显示 `2`。
- `samples/sample_conflicts.ndjson` —— 冲突样例：段内容冲突（2001）、总段数
  冲突（2002）、组装摘要校验不符（2003）、**先发布后收到篡改段而撤下观测**
  （2004）、整报摘要冲突（2005）。
- `samples/sample_baseline_101_2026-11-14T22.csv` —— 对应报文 1001 的 22 时
  基准：第 10、11 分钟故意超差，第 30–32 分钟故意缺失，其余合格。
- `samples/sample_baseline_101_2026-11-15T01.csv` —— 对应报文 1002 的 01 时
  基准，全部在阈值内。
- `python3 samples/generate_samples.py` 可重新生成以上文件。

按样例走一遍：先上传 `sample_upload.ndjson`，再上传两份基准，「4. 基准核验
结果」应显示 120 分钟：合格 115、超差 2、缺基准 3，核验 CSV 为 115 行数据。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 采集与核验网页 |
| POST | `/api/upload` | 上传 NDJSON（multipart 文件 `file` 或裸文本），返回逐行结果 |
| GET | `/api/messages` | 全部报文：已收段、缺段、状态、待核原因、观测数 |
| GET | `/api/observations?station_id=&limit=` | 已确认观测 JSON |
| GET | `/api/observations.csv?station_id=` | 原始已确认观测 CSV 下载 |
| POST | `/api/baselines` | 上传站号 + UTC 小时的基准 CSV（multipart `file`/`station_id`/`hour`），整小时替换 |
| GET | `/api/baselines?station_id=` | 当前各小时基准版本摘要 |
| GET | `/api/verification?station_id=&hour=` | 核验结果 JSON：摘要 + 逐分钟明细（含原报文行） |
| GET | `/api/verification.csv?station_id=&hour=` | 核验 CSV：仅 `|差值| ≤ 0.20 m` 的合格观测 |
| GET | `/api/verification/exceptions.csv?station_id=&hour=` | 缺基准 / 超差明细 CSV |
| DELETE | `/api/messages/{station}/{boot}/{msg}` | 复位报文（删除其段与观测） |

## 目录结构

```
app/
  main.py            FastAPI 路由与生命周期
  core.py            归组、组装、校验、解析、待核/撤下逻辑
  verification.py    基准 CSV 解析、整小时替换、配对核验与导出
  db.py              SQLite 连接与 schema
  static/index.html  采集与核验网页
samples/             样例生成器、样例 NDJSON 与基准 CSV
tests/               端到端测试（乱序/重投/冲突/撤下/重启/并发/基准核验）
Dockerfile, docker-compose.yml
```
