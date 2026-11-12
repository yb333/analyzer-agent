/**
 * Analyzer Agent 运营埋点接收服务端。
 *
 * 单文件实现：HTTP 服务 + SQLite 存储 + 聚合统计 + ECharts 看板。
 * 零外部依赖：node:sqlite（Node v22.5+ 内置）+ vendor/echarts.min.js（本地
 * 提供，内网无外网 CDN 也能打开看板）。
 *
 * 接口：
 *   POST /api/usage      接收单条或数组记录（INSERT OR IGNORE 幂等）
 *   GET  /api/stats      聚合统计 JSON
 *   GET  /api/health     健康检查
 *   GET  /               ECharts 看板
 *
 * 启动：
 *   npm install && npm start
 *   PORT=3000 node server.js
 */

const http = require('http');
const path = require('path');
const fs = require('fs');
// 用 Node 内置 node:sqlite（v22.5+ 自带，零外部依赖，无需编译）
// 替代 better-sqlite3 —— 后者是原生 C++ 模块，Windows 上常因缺编译工具链失败
const { DatabaseSync } = require('node:sqlite');

const PORT = parseInt(process.env.PORT || '3000', 10);

// 数据库路径：独立于代码目录，避免代码更新（git pull / 全量同步）覆盖用户数据。
// 默认放 ~/.analyzer-agent/（与客户端 usage.csv 同级），可用环境变量 USAGE_DB_PATH 覆盖。
const os = require('os');
const DEFAULT_DATA_DIR = path.join(os.homedir(), '.analyzer-agent');
const DATA_DIR = process.env.USAGE_DATA_DIR
  ? path.resolve(process.env.USAGE_DATA_DIR)
  : DEFAULT_DATA_DIR;
const DB_PATH = path.join(DATA_DIR, 'usage.db');

// ── 数据库初始化 ──────────────────────────────────────────────────────────

if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });

const db = new DatabaseSync(DB_PATH);
db.exec('PRAGMA journal_mode = WAL');
db.exec('PRAGMA busy_timeout = 5000');

// 固定列 schema（替代参考项目的 event+properties JSON 模式）
// user 是 SQL 保留字，列名用 user_name
db.exec(`
  CREATE TABLE IF NOT EXISTS usage_records (
    run_id TEXT PRIMARY KEY,
    trace_id TEXT,
    timestamp TEXT NOT NULL,
    install_id TEXT NOT NULL,
    user_name TEXT,
    command TEXT NOT NULL,
    input_type TEXT,
    asset TEXT,
    target_table TEXT,
    batch_id TEXT,
    rule_count INTEGER,
    field_count INTEGER,
    source_count INTEGER,
    elapsed_sec REAL,
    elapsed_detail TEXT,
    status TEXT NOT NULL,
    error_type TEXT,
    quality_issues INTEGER,
    error_message TEXT,
    agent_version TEXT,
    python_version TEXT,
    os TEXT,
    extra TEXT,
    received_at INTEGER NOT NULL
  );

  CREATE INDEX IF NOT EXISTS idx_usage_install_id ON usage_records(install_id);
  CREATE INDEX IF NOT EXISTS idx_usage_command ON usage_records(command);
  CREATE INDEX IF NOT EXISTS idx_usage_user ON usage_records(user_name);
  CREATE INDEX IF NOT EXISTS idx_usage_timestamp ON usage_records(timestamp);
  CREATE INDEX IF NOT EXISTS idx_usage_target ON usage_records(target_table);
  CREATE INDEX IF NOT EXISTS idx_usage_status ON usage_records(status);
`);

// ── 自动迁移：旧库升级时补新列（幂等，已有则跳过）──
try {
  const cols = db.prepare("PRAGMA table_info(usage_records)").all();
  const colNames = cols.map(c => c.name);
  if (!colNames.includes('elapsed_detail')) {
    db.exec('ALTER TABLE usage_records ADD COLUMN elapsed_detail TEXT');
    console.log('[Migration] Added column elapsed_detail');
  }
  if (!colNames.includes('trace_id')) {
    db.exec('ALTER TABLE usage_records ADD COLUMN trace_id TEXT');
    console.log('[Migration] Added column trace_id');
  }
  if (!colNames.includes('extra')) {
    db.exec('ALTER TABLE usage_records ADD COLUMN extra TEXT');
    console.log('[Migration] Added column extra');
  }
  if (!colNames.includes('error_message')) {
    db.exec('ALTER TABLE usage_records ADD COLUMN error_message TEXT');
    console.log('[Migration] Added column error_message');
  }
} catch (e) { /* 首次建表无此列检测可忽略 */ }

const INSERT_COLS = [
  'run_id', 'trace_id', 'timestamp', 'install_id', 'user_name', 'command', 'input_type',
  'asset', 'target_table', 'batch_id', 'rule_count', 'field_count',
  'source_count', 'elapsed_sec', 'elapsed_detail', 'status', 'error_type',
  'quality_issues', 'error_message', 'agent_version', 'python_version', 'os', 'extra'
];

const insertStmt = db.prepare(
  `INSERT OR IGNORE INTO usage_records (${INSERT_COLS.join(', ')}, received_at)
   VALUES (${INSERT_COLS.map(() => '?').join(', ')}, ?)`
);

// ── 静态资源（echarts 本地 vendor，启动时读入内存）────────────────────────
// 内网环境访问不了外网 CDN（cdn.jsdelivr.net 挂起等到超时），看板必须离线可用。
const VENDOR_ECHARTS = (() => {
  try {
    return fs.readFileSync(path.join(__dirname, 'vendor', 'echarts.min.js'));
  } catch (e) {
    console.warn('[WARN] vendor/echarts.min.js 缺失，看板图表将无法渲染');
    return null;
  }
})();

// ── /api/stats 聚合缓存 ──────────────────────────────────────────────────
// 多人/多标签页同时开看板时，避免每次刷新都重复跑 8 个全表聚合查询。
// TTL 内直接回内存结果；有新记录写入立即失效。
const STATS_TTL_MS = 10 * 1000;
let statsCache = { at: 0, data: null };

// ── HTTP 工具 ────────────────────────────────────────────────────────────

function corsHeaders() {
  return {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
  };
}

function jsonReply(res, code, data) {
  const body = JSON.stringify(data);
  res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8', ...corsHeaders() });
  res.end(body);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf-8')));
    req.on('error', reject);
  });
}

function toInt(v) {
  const n = parseInt(v, 10);
  return Number.isFinite(n) ? n : null;
}
function toFloat(v) {
  const n = parseFloat(v);
  return Number.isFinite(n) ? n : null;
}
function toStr(v) {
  return v === undefined || v === null ? null : String(v);
}

// ── POST /api/usage ──────────────────────────────────────────────────────

function handlePostUsage(payload) {
  // 支持单条对象或数组
  let records;
  if (Array.isArray(payload)) {
    records = payload;
  } else if (payload && typeof payload === 'object') {
    records = [payload];
  } else {
    return { statusCode: 400, body: { ok: false, error: 'Invalid payload' } };
  }
  if (records.length === 0) {
    return { statusCode: 400, body: { ok: false, error: 'Empty records' } };
  }

  const now = Date.now();
  let received = 0;

  // 已知固定列（用于判断哪些字段该进 extra）
  const KNOWN_COLS = new Set(INSERT_COLS.concat(['user']));

  // node:sqlite 无 db.transaction() helper，手动 BEGIN/COMMIT/ROLLBACK
  try {
    db.exec('BEGIN');
    for (const r of records) {
      if (!r || !r.run_id || !r.command) continue;
      // 收集未知字段进 extra（扩展时新字段自动存，不改 schema）
      let extra = {};
      try { extra = r.extra ? JSON.parse(r.extra) : {}; } catch (e) { extra = {}; }
      for (const k of Object.keys(r)) {
        if (!KNOWN_COLS.has(k) && r[k] !== null && r[k] !== undefined && r[k] !== '') {
          extra[k] = r[k];
        }
      }
      const result = insertStmt.run(
        toStr(r.run_id), toStr(r.trace_id), toStr(r.timestamp), toStr(r.install_id),
        toStr(r.user_name || r.user),
        toStr(r.command), toStr(r.input_type), toStr(r.asset), toStr(r.target_table),
        toStr(r.batch_id), toInt(r.rule_count), toInt(r.field_count), toInt(r.source_count),
        toFloat(r.elapsed_sec), toStr(r.elapsed_detail), toStr(r.status) || 'unknown',
        toStr(r.error_type), toInt(r.quality_issues), toStr(r.error_message), toStr(r.agent_version),
        toStr(r.python_version), toStr(r.os),
        Object.keys(extra).length ? JSON.stringify(extra) : null, now
      );
      if (result.changes > 0) received++;
    }
    db.exec('COMMIT');
  } catch (err) {
    try { db.exec('ROLLBACK'); } catch (e) { /* ignore */ }
    return { statusCode: 500, body: { ok: false, error: 'Database error: ' + err.message } };
  }

  if (received > 0) statsCache = { at: 0, data: null }; // 有新数据，统计缓存失效

  const ts = new Date().toISOString();
  console.log(`[${ts}] Received ${received}/${records.length} records`);
  return { statusCode: 200, body: { ok: true, received } };
}

// ── GET /api/health ──────────────────────────────────────────────────────

function handleHealth() {
  let size = 0;
  try {
    size = fs.statSync(DB_PATH).size;
    // 加上 WAL 文件
    const walPath = DB_PATH + '-wal';
    if (fs.existsSync(walPath)) size += fs.statSync(walPath).size;
  } catch (e) { /* ignore */ }
  return { status: 'ok', uptime: Math.floor(process.uptime()), db_size_mb: Math.round(size / 1048576 * 100) / 100 };
}

// ── GET /api/stats ───────────────────────────────────────────────────────

function handleStats() {
  if (statsCache.data && Date.now() - statsCache.at < STATS_TTL_MS) {
    return statsCache.data;
  }
  const result = {};

  // 1. 概览
  // 用户行为数 = 按 trace_id 去重（一次完整分析算1次）；无 trace_id 的按 run_id 算
  // 各阶段调用次数 = 全部记录数（解析和视图各自计次）
  result.overview = db.prepare(`
    SELECT
      COUNT(DISTINCT install_id) AS total_users,
      COUNT(DISTINCT CASE WHEN trace_id IS NOT NULL AND trace_id != '' THEN trace_id ELSE run_id END) AS total_actions,
      COUNT(*) AS total_records,
      COUNT(DISTINCT CASE WHEN status='ok' THEN run_id END) AS total_ok,
      COUNT(DISTINCT CASE WHEN status='error' THEN run_id END) AS total_error,
      ROUND(AVG(elapsed_sec), 2) AS avg_elapsed_sec,
      SUM(CASE WHEN quality_issues > 0 THEN quality_issues ELSE 0 END) AS total_quality_issues,
      SUM(CASE WHEN json_extract(extra, '$.caller') = 'agent' THEN 1 ELSE 0 END) AS agent_calls,
      MIN(timestamp) AS first_event,
      MAX(timestamp) AS last_event
    FROM usage_records
  `).get();

  // 2. 日活趋势（全量按天返回——前端 30/90/全部 本地切换，不用重新请求）
  // users = 安装实例数，actions = 用户行为数（去重）
  result.daily_active = db.prepare(`
    SELECT
      substr(timestamp, 1, 10) AS date,
      COUNT(DISTINCT install_id) AS users,
      COUNT(DISTINCT CASE WHEN trace_id IS NOT NULL AND trace_id != '' THEN trace_id ELSE run_id END) AS actions
    FROM usage_records
    GROUP BY date
    ORDER BY date DESC
  `).all();

  // 3. 命令热度（各阶段各自计次 —— 看脚本被调多少次；agent_count = agent 调用占比分母分子）
  result.command_usage = db.prepare(`
    SELECT command, COUNT(*) AS count,
           SUM(CASE WHEN json_extract(extra, '$.caller') = 'agent' THEN 1 ELSE 0 END) AS agent_count,
           MAX(timestamp) AS last_used
    FROM usage_records GROUP BY command ORDER BY count DESC
  `).all();

  // 4. 用户活跃度（按 install_id 去重，显示工号）
  result.instance_activity = db.prepare(`
    SELECT
      install_id,
      COALESCE(MAX(user_name), '(未识别)') AS user_name,
      COUNT(DISTINCT CASE WHEN trace_id IS NOT NULL AND trace_id != '' THEN trace_id ELSE run_id END) AS actions,
      COUNT(*) AS records,
      MAX(timestamp) AS last_active
    FROM usage_records GROUP BY install_id ORDER BY actions DESC LIMIT 50
  `).all();

  // 5. 资产分析 Top10（按 trace_id 去重 —— 一次完整分析算1次）
  // 注意 GROUP BY 用 asset_name 别名而非 asset —— 与表列 asset 同名时
  // SQLite 按原始列分组，asset 列为 NULL 的行会被错误合并成一组
  result.top_assets = db.prepare(`
    SELECT
      COALESCE(target_table, asset, '(unknown)') AS asset_name,
      COUNT(DISTINCT CASE WHEN trace_id IS NOT NULL AND trace_id != '' THEN trace_id ELSE run_id END) AS count,
      MAX(timestamp) AS last_analyzed
    FROM usage_records
    WHERE target_table IS NOT NULL OR asset IS NOT NULL
    GROUP BY asset_name ORDER BY count DESC LIMIT 10
  `).all();

  // 错误详情（最近 20 条含 error_message 的错误记录）
  result.error_details = db.prepare(`
    SELECT
      error_type,
      error_message,
      command,
      target_table,
      user_name,
      timestamp
    FROM usage_records
    WHERE status = 'error' AND error_message IS NOT NULL AND error_message != ''
    ORDER BY timestamp DESC
    LIMIT 20
  `).all();

  // 7. 输入类型占比（只统计有用户输入的命令，排除 view-generator）
  result.input_types = db.prepare(`
    SELECT
      COALESCE(input_type, '(未记录)') AS input_type,
      COUNT(*) AS count
    FROM usage_records
    WHERE command != 'view-generator'
    GROUP BY input_type ORDER BY count DESC
  `).all();

  // 8. 版本分布
  result.versions = db.prepare(`
    SELECT
      COALESCE(agent_version, '(unknown)') AS agent_version,
      COUNT(DISTINCT install_id) AS users,
      COUNT(*) AS count
    FROM usage_records GROUP BY agent_version ORDER BY count DESC
  `).all();

  // 9. 质量问题 Top 资产（质量发现是分析器核心产出，看哪些资产最需要治理）
  // GROUP BY 同上用 asset_name 别名，防原始列 NULL 时错误合并
  result.quality_top_assets = db.prepare(`
    SELECT
      COALESCE(target_table, asset, '(unknown)') AS asset_name,
      SUM(quality_issues) AS issues,
      COUNT(*) AS analyses
    FROM usage_records
    WHERE quality_issues > 0
    GROUP BY asset_name ORDER BY issues DESC LIMIT 10
  `).all();

  // 10. 耗时分位数（平均易被极端值拉爆：子命令级 P50<0.1s，极值可到分钟级）
  const elapsed = db.prepare(
    'SELECT elapsed_sec FROM usage_records WHERE elapsed_sec IS NOT NULL'
  ).all().map(r => r.elapsed_sec).sort((a, b) => a - b);
  const pct = (p) => elapsed.length
    ? Math.round(elapsed[Math.min(elapsed.length - 1, Math.floor(elapsed.length * p))] * 100) / 100
    : null;
  result.elapsed_pct = { p50: pct(0.50), p90: pct(0.90), p99: pct(0.99), n: elapsed.length };

  // 11. 错误归类（error_type 分布；各命令错误路径均有埋点，数据到来即显色）
  result.error_types = db.prepare(`
    SELECT COALESCE(NULLIF(error_type, ''), '(未分类)') AS error_type,
           COUNT(*) AS count,
           MAX(timestamp) AS last_seen
    FROM usage_records
    WHERE status = 'error'
    GROUP BY error_type ORDER BY count DESC
  `).all();

  // 12. 慢任务 Top10（性能优化指引：哪个资产/命令最慢）
  result.slowest_tasks = db.prepare(`
    SELECT command,
           COALESCE(NULLIF(target_table, ''), NULLIF(asset, ''), '(未知)') AS asset_name,
           ROUND(elapsed_sec, 2) AS elapsed_sec,
           timestamp, user_name
    FROM usage_records
    WHERE elapsed_sec IS NOT NULL
    ORDER BY elapsed_sec DESC LIMIT 10
  `).all();

  // 13. 阶段耗时分解（analyze 五阶段平均耗时——看瓶颈在哪一环）
  result.stage_breakdown = db.prepare(`
    SELECT
      ROUND(AVG(json_extract(elapsed_detail, '$.parse')), 3) AS parse,
      ROUND(AVG(json_extract(elapsed_detail, '$.ddl_discovery')), 3) AS ddl_discovery,
      ROUND(AVG(json_extract(elapsed_detail, '$.lts_discovery')), 3) AS lts_discovery,
      ROUND(AVG(json_extract(elapsed_detail, '$.write_json_and_summary')), 3) AS write_out,
      ROUND(AVG(json_extract(elapsed_detail, '$.view_generation')), 3) AS view_gen
    FROM usage_records
    WHERE elapsed_detail IS NOT NULL AND elapsed_detail != ''
  `).get();

  statsCache = { at: Date.now(), data: result };
  return result;
}

// ── GET / 看板 ────────────────────────────────────────────────────────────

function buildDashboardHtml() {
  const html = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Analyzer Agent 运营数据</title>
<script src="/vendor/echarts.min.js"><\/script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; background: #f5f7fa; color: #333; padding: 20px; }
  h1 { font-size: 22px; margin-bottom: 4px; }
  .subtitle { color: #888; font-size: 13px; margin-bottom: 20px; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 16px; margin-bottom: 24px; }
  .card { background: #fff; border-radius: 10px; padding: 18px; box-shadow: 0 1px 4px rgba(0,0,0,0.06); }
  .card .label { font-size: 12px; color: #888; margin-bottom: 6px; }
  .card .value { font-size: 28px; font-weight: 600; color: #1a73e8; }
  .card .sub { font-size: 11px; color: #aaa; margin-top: 4px; }
  .section { background: #fff; border-radius: 10px; padding: 18px; margin-bottom: 20px; box-shadow: 0 1px 4px rgba(0,0,0,0.06); }
  .section h2 { font-size: 15px; margin-bottom: 12px; border-left: 3px solid #1a73e8; padding-left: 8px; }
  .chart { width: 100%; height: 320px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { padding: 8px 10px; text-align: left; border-bottom: 1px solid #eee; }
  th { color: #888; font-weight: 500; background: #fafbfc; }
  tr:hover { background: #fafbfc; }
  .bar-cell { position: relative; min-width: 120px; }
  .bar-fill { background: #1a73e8; height: 6px; border-radius: 3px; margin-top: 4px; }
  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
  @media (max-width: 768px) { .grid-2 { grid-template-columns: 1fr; } }
  #loading { text-align: center; padding: 60px; color: #888; }
  .range-btns { float: right; font-weight: normal; }
  .range-btns button { border: 1px solid #ddd; background: #fff; color: #666; font-size: 12px; padding: 3px 10px; border-radius: 4px; cursor: pointer; margin-left: 4px; }
  .range-btns button.active { background: #1a73e8; color: #fff; border-color: #1a73e8; }
</style>
</head>
<body>
<h1>Analyzer Agent 运营数据</h1>
<div class="subtitle" id="lastUpdate">加载中...</div>

<div id="loading">正在加载...</div>
<div id="content" style="display:none">

  <div class="cards" id="overviewCards"></div>

  <div class="section">
    <h2>日活趋势
      <span class="range-btns">
        <button class="active" onclick="setDailyRange(30, this)">近30天</button>
        <button onclick="setDailyRange(90, this)">近90天</button>
        <button onclick="setDailyRange(0, this)">全部</button>
      </span>
    </h2>
    <div id="dailyChart" class="chart"></div>
  </div>

  <div class="grid-2">
      <div class="section">
      <h2>命令热度</h2>
      <table><thead><tr><th>命令</th><th>次数</th><th>agent 占比</th><th>最后使用</th></tr></thead><tbody id="cmdTable"></tbody></table>
    </div>
    <div class="section">
      <h2>输入类型占比</h2>
      <div id="inputChart" class="chart" style="height:260px"></div>
    </div>
  </div>

  <div class="grid-2">
    <div class="section">
      <h2>用户活跃度</h2>
      <div id="userActivityChart" class="chart"></div>
    </div>
    <div class="section">
      <h2>资产分析 Top10（去重）</h2>
      <table><thead><tr><th>资产</th><th>分析次数</th><th>最后分析</th></tr></thead><tbody id="assetTable"></tbody></table>
    </div>
  </div>

  <div class="grid-2">
    <div class="section">
      <h2>质量问题 Top 资产</h2>
      <table><thead><tr><th>资产</th><th>问题数</th><th>分析次数</th></tr></thead><tbody id="qualityTable"></tbody></table>
    </div>
    <div class="section">
      <h2>版本分布</h2>
      <div id="versionChart" class="chart" style="height:260px"></div>
    </div>
  </div>

  <div class="grid-2">
    <div class="section">
      <h2>慢任务 Top10</h2>
      <table><thead><tr><th>命令</th><th>资产</th><th>耗时</th><th>时间</th><th>用户</th></tr></thead><tbody id="slowTable"></tbody></table>
    </div>
    <div class="section">
      <h2>错误归类</h2>
      <table><thead><tr><th>类型</th><th>次数</th><th>最后出现</th></tr></thead><tbody id="errorTypeTable"></tbody></table>
    </div>
  </div>

  <div class="section">
    <h2>阶段耗时分解（analyze 平均）</h2>
    <div id="stageChart" class="chart" style="height:260px"></div>
  </div>

  <div class="section">
    <h2>错误详情（最近 20 条）</h2>
    <table><thead><tr>
      <th>时间</th><th>类型</th><th>命令</th><th>资产</th><th>用户</th><th>错误信息</th>
    </tr></thead><tbody id="errorDetailTable"></tbody></table>
  </div>

</div>

<script>
// 命令名中文映射 —— 只做翻译，新命令不在这里也能正常显示（显示英文原名）
// 扩展时加新命令，想显示中文名就在这里加一行（不加也不影响功能）
var COMMAND_NAMES = {
  'analyze': '资产文档化',
  'analyze-chain': '多规则组链路分析',
  'analyze-batch': '批量文档化',
  'view-generator': '视图生成',
  'impact-analysis': '关联影响分析',
  'impact-analysis-cross': '跨资产影响分析',
  'field-search': '字段使用检索',
  'facts': 'ts草稿导出（存量契约）',
  'spec': '需求SPEC导出'
};
var INPUT_NAMES = {
  'xlsx': '术加制品包Excel',
  'yml_dir': '代码仓yml目录',
  'table_name': '仅表名',
  'impact_excel': '变更清单Excel',
  'field_list': '字段列表',
  'knowledge_json': '解析结果JSON(view-generator)',
  'artifact_dir': '制品目录'
};

function fmtTime(s) {
  if (!s) return '-';
  return s.replace('T', ' ').substring(0, 19);
}
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g, function(c) {
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];
  });
}
function cmdLabel(c) { return COMMAND_NAMES[c] || c || '(unknown)'; }
function inputLabel(i) { return INPUT_NAMES[i] || i || '(未记录)'; }

function renderOverview(o, ep) {
  var html = '';
  html += card('用户数', o.total_users || 0, '按 install_id 去重');
  html += card('分析行为数', o.total_actions || 0, '一次完整分析=1次（去重）');
  html += card('脚本调用数', o.total_records || 0,
    '成功 ' + (o.total_ok||0) + ' / 失败 ' + (o.total_error||0));
  var agentCalls = o.agent_calls || 0;
  var agentPct = o.total_records ? Math.round(agentCalls * 100 / o.total_records) : 0;
  html += card('agent 调用占比', agentPct + '%', agentCalls + ' 次 / 共 ' + (o.total_records || 0) + ' 次');
  html += card('耗时 P50 / P90',
    ((ep && ep.p50 != null ? ep.p50 : 0) + 's / ' + (ep && ep.p90 != null ? ep.p90 : 0) + 's'),
    'P99 ' + (ep && ep.p99 != null ? ep.p99 : '-') + 's · 平均 ' + (o.avg_elapsed_sec || 0) + 's · n=' + (ep ? ep.n : 0));
  html += card('质量问题', o.total_quality_issues || 0, '累计发现（汇总 quality_issues）');
  html += card('最后活跃', fmtTime(o.last_event).substring(0, 16), '');
  document.getElementById('overviewCards').innerHTML = html;
}
function card(label, value, sub) {
  return '<div class="card"><div class="label">' + label + '</div>' +
    '<div class="value">' + value + '</div>' +
    (sub ? '<div class="sub">' + sub + '</div>' : '') + '</div>';
}

// ── 图表实例池（性能关键）─────────────────────────────────────────────
// 同一容器只 init 一次：反复 echarts.init 同一 DOM 会泄漏实例（canvas 和
// 动画 ticker 不释放），看板长开 + 60s 自动刷新会累积数千个实例，浏览器
// 内存/GPU 持续上涨导致整机越来越卡。resize 监听同理只注册一次。
var charts = [];
window.addEventListener('resize', function () {
  for (var i = 0; i < charts.length; i++) charts[i].resize();
}, { passive: true });

function getChart(id) {
  var el = document.getElementById(id);
  if (!el) return null;
  var c = echarts.getInstanceByDom(el);
  if (!c) { c = echarts.init(el); charts.push(c); }
  return c;
}

// 空数据占位用 graphic 文本，不用 innerHTML 清容器（清 DOM 会让存活实例失效）
var EMPTY_OPT = {
  graphic: [{ type: 'text', left: 'center', top: 'middle',
    style: { text: '暂无数据', fill: '#aaa', fontSize: 14 } }]
};

// 日活时间范围：30 / 90 / 0（全部）。切范围用已拉取的全量数据本地过滤，不发请求
var dailyRangeDays = 30;
var lastStats = null;

function setDailyRange(days, btn) {
  dailyRangeDays = days;
  var btns = document.querySelectorAll('.range-btns button');
  for (var i = 0; i < btns.length; i++) btns[i].classList.toggle('active', btns[i] === btn);
  if (lastStats) renderDaily(lastStats.daily_active || []);
}

function renderDaily(all) {
  var chart = getChart('dailyChart');
  if (!chart) return;
  var da = dailyRangeDays > 0 ? all.slice(0, dailyRangeDays) : all; // all 按日期 DESC，前 N 即最近 N 天
  var dates = da.map(function(r){return r.date;}).reverse();
  var users = da.map(function(r){return r.users;}).reverse();
  var actions = da.map(function(r){return r.actions;}).reverse();
  chart.setOption({
    tooltip: { trigger: 'axis' },
    legend: { data: ['实例数', '行为数'] },
    grid: { left: 40, right: 40, bottom: 40, top: 40 },
    xAxis: { type: 'category', data: dates, axisLabel: { rotate: 45, fontSize: 10 } },
    yAxis: [
      { type: 'value', name: '实例数', position: 'left' },
      { type: 'value', name: '行为数', position: 'right' }
    ],
    series: [
      { name: '实例数', type: 'line', smooth: true, data: users,
        itemStyle: { color: '#1a73e8' }, areaStyle: { opacity: 0.1 } },
      { name: '行为数', type: 'line', smooth: true, yAxisIndex: 1, data: actions,
        itemStyle: { color: '#34a853' }, areaStyle: { opacity: 0.1 } }
    ]
  }, true); // notMerge：全量替换，防旧系列残留
}

function renderTable(id, rows, mapper) {
  var el = document.getElementById(id);
  if (!el) return;
  var html = rows.length ? rows.map(function(r) {
    return '<tr>' + mapper(r).map(function(c){return '<td>'+esc(c)+'</td>';}).join('') + '</tr>';
  }).join('') : '<tr><td colspan="9" style="color:#aaa;text-align:center;padding:20px">暂无数据</td></tr>';
  el.innerHTML = html;
}

function renderPie(id, data, nameMapper) {
  var chart = getChart(id);
  if (!chart) return;
  if (!data.length) { chart.setOption(EMPTY_OPT, true); return; }
  var pieData = data.map(function(d) {
    return { name: nameMapper(d[Object.keys(d)[0]]), value: d.count };
  });
  chart.setOption({
    tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
    series: [{
      type: 'pie', radius: ['40%', '70%'], data: pieData,
      label: { fontSize: 11 }
    }]
  }, true);
}

function renderUserActivity(instances) {
  var chart = getChart('userActivityChart');
  if (!chart) return;
  if (!instances.length) { chart.setOption(EMPTY_OPT, true); return; }
  // 取 Top10，横向柱状图：行为数 + 调用次数 双系列
  var top = instances.slice(0, 10);
  var names = top.map(function(r){ return r.user_name || '(未识别)'; });
  chart.setOption({
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    legend: { data: ['行为数', '调用次数'] },
    grid: { left: 90, right: 30, top: 40, bottom: 20 },
    xAxis: { type: 'value' },
    yAxis: { type: 'category', data: names, inverse: true, axisLabel: { fontSize: 11 } },
    series: [
      { name: '行为数', type: 'bar', data: top.map(function(r){return r.actions;}),
        itemStyle: { color: '#1a73e8' }, barGap: '10%' },
      { name: '调用次数', type: 'bar', data: top.map(function(r){return r.records;}),
        itemStyle: { color: '#34a853' } }
    ]
  }, true);
}

// analyze 五阶段中文名（阶段耗时分解用；键对齐服务端 stage_breakdown）
var STAGE_NAMES = {
  parse: 'SQL 解析',
  ddl_discovery: 'DDL 发现',
  lts_discovery: 'LTS 发现',
  write_out: 'JSON/摘要落盘',
  view_gen: '视图生成'
};

function renderStageBreakdown(sb) {
  var chart = getChart('stageChart');
  if (!chart) return;
  var keys = ['parse', 'ddl_discovery', 'lts_discovery', 'write_out', 'view_gen'];
  var names = [], vals = [];
  // 倒序填充：横条 category 轴自下而上，倒序后 parse 显示在最上（符合流程序）
  for (var i = keys.length - 1; i >= 0; i--) {
    var v = sb[keys[i]];
    names.push(STAGE_NAMES[keys[i]]);
    vals.push(v == null ? 0 : v);
  }
  chart.setOption({
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' },
      formatter: function(p) { return p[0].name + ': ' + p[0].value + 's'; } },
    grid: { left: 110, right: 60, top: 20, bottom: 30 },
    xAxis: { type: 'value', axisLabel: { formatter: '{value}s' } },
    yAxis: { type: 'category', data: names, axisLabel: { fontSize: 12 } },
    series: [{ type: 'bar', data: vals, barMaxWidth: 22,
      itemStyle: { color: '#1a73e8' },
      label: { show: true, position: 'right', formatter: '{c}s', fontSize: 11 } }]
  }, true);
}

function render(data) {
  lastStats = data;
  renderOverview(data.overview || {}, data.elapsed_pct || {});
  renderDaily(data.daily_active || []);
  renderTable('cmdTable', data.command_usage || [], function(r) {
    var pct = r.agent_count ? Math.round(r.agent_count * 100 / r.count) + '%' : '-';
    return [cmdLabel(r.command), r.count, pct, fmtTime(r.last_used)];
  });
  renderTable('assetTable', data.top_assets || [], function(r) {
    return [r.asset_name, r.count, fmtTime(r.last_analyzed)];
  });
  renderPie('inputChart', data.input_types || [], inputLabel);
  renderUserActivity(data.instance_activity || []);
  renderTable('qualityTable', data.quality_top_assets || [], function(r) {
    return [r.asset_name, r.issues, r.analyses];
  });
  renderPie('versionChart', data.versions || [], function(v){ return v; });
  renderTable('slowTable', data.slowest_tasks || [], function(r) {
    return [cmdLabel(r.command), r.asset_name, (r.elapsed_sec || 0) + 's',
            fmtTime(r.timestamp).substring(0, 16), r.user_name || '-'];
  });
  renderTable('errorTypeTable', data.error_types || [], function(r) {
    return [r.error_type, r.count, fmtTime(r.last_seen).substring(0, 16)];
  });
  renderStageBreakdown(data.stage_breakdown || {});
  renderTable('errorDetailTable', data.error_details || [], function(r) {
    return [
      fmtTime(r.timestamp).substring(0, 16),
      r.error_type || '-',
      r.command || '-',
      (r.target_table || '-').substring(0, 25),
      r.user_name || '-',
      '<span style="font-size:11px;color:#e53935;">' + esc(r.error_message || '-') + '</span>'
    ];
  });
}

function refresh() {
  if (document.hidden) return; // 后台标签页不刷新（省 CPU），回前台立刷
  fetch('/api/stats').then(function(r){return r.json();}).then(function(data){
    document.getElementById('loading').style.display = 'none';
    document.getElementById('content').style.display = 'block';
    document.getElementById('lastUpdate').textContent = '更新于 ' + new Date().toLocaleTimeString('zh-CN');
    setTimeout(function(){
      try { render(data); }
      catch (e) {
        var el = document.getElementById('loading');
        if (el) { el.style.display = 'block'; el.textContent = '渲染失败: ' + e.message; }
      }
    }, 0);
  }).catch(function(err){
    document.getElementById('loading').textContent = '加载失败: ' + err.message;
  });
}
refresh();
setInterval(refresh, 60000);
// 切回前台立即刷新（补上后台期间跳过的数据）
document.addEventListener('visibilitychange', function () {
  if (!document.hidden) refresh();
});
<\/script>
</body>
</html>`;
  return html;
}

// 看板 HTML 启动时构建一次（内容静态，无需每个请求重建大字符串）
const DASHBOARD_HTML = buildDashboardHtml();

// ── 路由分发 ──────────────────────────────────────────────────────────────

const server = http.createServer(async (req, res) => {
  // CORS preflight
  if (req.method === 'OPTIONS') {
    res.writeHead(204, corsHeaders());
    res.end();
    return;
  }

  const url = new URL(req.url, 'http://localhost');
  const pathname = url.pathname;

  if (req.method === 'POST' && pathname === '/api/usage') {
    const raw = await readBody(req);
    let payload;
    try {
      payload = JSON.parse(raw);
    } catch (e) {
      jsonReply(res, 400, { ok: false, error: 'Invalid JSON: ' + e.message });
      return;
    }
    const result = handlePostUsage(payload);
    jsonReply(res, result.statusCode, result.body);
    return;
  }

  if (req.method === 'GET' && pathname === '/api/stats') {
    try {
      jsonReply(res, 200, handleStats());
    } catch (e) {
      jsonReply(res, 500, { ok: false, error: e.message });
    }
    return;
  }

  if (req.method === 'GET' && pathname === '/api/health') {
    jsonReply(res, 200, handleHealth());
    return;
  }

  if (req.method === 'GET' && pathname === '/vendor/echarts.min.js') {
    if (VENDOR_ECHARTS) {
      res.writeHead(200, {
        'Content-Type': 'application/javascript; charset=utf-8',
        'Cache-Control': 'public, max-age=86400', // 看板标签页长开也只拉一次
      });
      res.end(VENDOR_ECHARTS);
    } else {
      res.writeHead(404, { 'Content-Type': 'text/plain' });
      res.end('vendor/echarts.min.js missing');
    }
    return;
  }

  if (req.method === 'GET' && (pathname === '/' || pathname === '/index.html')) {
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    res.end(DASHBOARD_HTML);
    return;
  }

  jsonReply(res, 404, { error: 'Not found: ' + pathname });
});

server.listen(PORT, '0.0.0.0', () => {
  console.log(`[Analyzer Agent Telemetry] 监听 http://0.0.0.0:${PORT}`);
  console.log(`  看板:    http://localhost:${PORT}/`);
  console.log(`  上报:    POST http://<server-ip>:${PORT}/api/usage`);
  console.log(`  统计:    GET  http://localhost:${PORT}/api/stats`);
  console.log(`  数据库:  ${DB_PATH}`);
});
