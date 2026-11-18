# 考勤跨夜班与加班分段核算

面向制造企业薪酬核算员，处理采信、判定、核算、复核与归档。

## 启动

```bash
docker compose up --build -d
```

访问 http://localhost:18252 。初次启动自动运行脏样例，可切换 clean.csv 与 legacy.csv。顶部“运行批次”会重新计算，同源输入复用批次。

## 业务模块

- 打卡采信：`/m1_ingest`、`/api/m1_ingest`
- 班次日历：`/m2_mapping`、`/api/m2_mapping`
- 跨夜配对：`/m3_versions`、`/api/m3_versions`
- 缺卡复核：`/m4_segments`、`/api/m4_segments`
- 工时切段：`/m5_calculate`、`/api/m5_calculate`
- 薪资对账：`/m6_reconcile`、`/api/m6_reconcile`
- 周期归档：`/m7_archive`、`/api/m7_archive`

## 核心规则

跨夜按班次归属日配对，休息区间先扣除，再按法定日期和工时阈值切分倍率。完整数值、边界和异常定义见 `docs/DATA-DICTIONARY.md`。

## 验证

```bash
docker compose exec -T app pytest -q
docker compose config --quiet
```

原始样例在 `sample_data/`。测试在 `tests/`。封存文件是单份CSV，含运行清单、周期采用版本、计算证据、延后补卡、跨期调整、隔离原因与对账。复核需提交当前版本，陈旧复核返回409。

## 周期封账与晚到补卡

月底封账前常收到晚到补卡。本项目的裁决是：**已复核并封账的周期保持原采用版本，晚到补卡不回溯改写已复核工资，登记到延后账留到下一期采用。**

- 大额改动集中在已复核工资时，走下一期机制，不在本期覆盖。`/m7_archive` 的“封账”冻结当前复核版本，之后复核返回 `PERIOD_CLOSED`。
- 同一班次只采用相互一致的进出卡：进卡或出卡任一改版即整张卡延后，本期仍按冻结卡复算，不拼接新旧卡。
- 被挡下的补卡保留原件、来源行与稳定键，重跑不新增重复记录；下期带入时与该班次上期冻结卡配对，已付金额保留，本期只计补发或补扣差额。
- 页面（跨夜配对、工时切段、薪资对账、周期归档）与导出CSV都会显示本期采用的班次版本、来源，以及被挡下的补卡与去向。
- 封账失败不写入，旧版证据`/api/periods/<period>/evidence.csv`保持可取。

封账与延后账接口：`GET /api/periods`、`POST /api/periods/<period>/close`、`GET /api/periods/<period>/evidence.csv`。

## 数据与限制

SQLite保存在records命名卷，普通重启与构建不会清除。确认可丢弃本项目数据后，运行 `docker compose down -v` 再启动可重置。单文件上限2MB；不接入外部业务系统；历史规则由本项目样例定义，不代表实际行业标准。
