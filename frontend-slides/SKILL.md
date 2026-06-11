---
name: frontend-slides
description: Create HTML presentations for slides, reports, or visual summaries. ALWAYS USE THIS SKILL when the user asks for PPT, slides, 洞察报告, or any visual presentation. Uses inline SVG for all architecture diagrams, flow charts, and visual elements (NO emoji or ASCII art for diagrams). Default style: adaptive viewport, light theme, large fonts, minimal whitespace. Output single HTML file and send via message tool.
---

# Frontend Slides Skill

**默认配置（老板偏好）**：
- **尺寸**：自适应浏览器窗口（100vw × 100vh）
- **主题**：亮色（白底 + 蓝色系）
- **字体**：大字体（20px+ 正文，32px+ 标题），所有尺寸用 vw 单位
- **布局**：少空白，内容填满，工整矩形，工整对齐
- **输出**：单 HTML 文件，用 message 发飞书，不本地打开
- **交付**：12点前直接发文件路径，老板自己打开

---

## 老板 PPT 制作规范（必查清单）

### 设计原则（从实战中总结）

#### 布局设计原则

| 原则 | 说明 |
|------|------|
| 先定比例再填内容 | 每页先确定各区块空间占比（如顶部60%、底部40%），再填充内容。不要写完内容再调布局 |
| 类比比数据先声夺人 | 管理层先理解"这是什么"再看数字。类比选"赋能"类（CPU+OS）不选"驯服"类（缰绳马鞍） |
| 层次感 > 平铺 | 每层只做一件事，不要混在一起。理解→方案→效果，逐层递进 |
| 空容器必须有内容 | 每个视觉区域都应承载信息，空着就是浪费。漏斗里放递减数字，卡片里放 SVG 小图 |
| 从"列清单"到"讲故事" | 问题→解法→效果，不要只罗列。"6个办法"不如"从400K砍到53K的6刀" |

#### SVG 布局铁律

| 规则 | 说明 |
|------|------|
| 整体移动用 transform | 移动整组元素用 `<g transform="translate(x,y)">`，**禁止逐元素改坐标**（会改乱不相关的元素） |
| 改内容必须同步调容器 | 增删 SVG 内容后，viewBox 和外层容器尺寸**必须同步更新**。加了文字要加高容器，删了内容要回收空间 |
| 标签重叠时错开放 | 多个标注不能挤同一 y 坐标。一个放上，一个放下，给每个信息要素分配独立的视觉空间 |
| 区域必须精确包裹内容 | 三色区域的 x/width 要和内部节点坐标严格对应，不能偏移超出 |
| 容器适配内容 | 卡片高度应该自适应内容，不是固定值。内容少的卡片就该矮，不要强行统一高度 |

#### 字号与视觉层级

| 规则 | 说明 |
|------|------|
| 关键信息不能用小灰字 | 写了就要让人看得见，看不见不如不写。说明文字用 12px+ 加粗，不要缩成 8px 灰色 |
| 字号先定范围再微调 | 投影可读下限 1.1-1.2vw，在这个范围内调整内容长度，而不是反过来 |
| 关键数字放大加粗变色 | 百分比和倍数天然吸引注意力。单独呈现、用醒目颜色（紫/绿）、≥14px |
| 字号是试出来的 | 没有一次到位的字号。给范围让用户选，而不是自己猜最终值 |
| 投影最小字号参考 | SVG 内文字 ≥11px，卡片描述 ≥1.2vw，图例/脚注 ≥9px |

#### SVG 字号放大映射表（基准→放大）

| 原始 | 放大后 | 用途 |
|------|-------|------|
| 7 | 8 | 极小标注 |
| 8 | 9 | 辅助说明 |
| 9 | 11 | 小标签 |
| 10 | 12 | 正文副标题 |
| 11 | 13 | 正文 |
| 12 | 14 | 小标题 |
| 13 | 15 | 模块标题 |
| 14 | 16 | 区域标题 |

> CSS 变量同步放大：`--body-size` +15%，`--small-size` +15%，`--quote-size` +15%

#### 图表选择指南

| 场景 | 推荐图表 | 理由 |
|------|---------|------|
| 递减/削减过程 | 瀑布递减图 | 比堆叠并列更能讲故事，视觉直观展示"越砍越小" |
| 逐层过滤/拦截 | 漏斗图 | 视觉天然传达"越来越少"，漏斗内放递减数字 |
| 对比/差异 | 左右对照卡片 | Demo vs Production，问题 vs 方案 |
| 流程/流水线 | 横向流程图 | 三色区域划分 + 节点 + 箭头，区域内节点均匀分布 |
| 数据要有参照物 | 进度条 + 安全线 | 光放绝对数字没感觉，要和基准线/安全线对比 |

#### 内容叙事规则

| 规则 | 说明 |
|------|------|
| "怎么做的"比"做了什么"更重要 | 每层方案都要有人话说明具体做法，管理层看到"L1 流程分解"四个字不知道怎么实现 |
| 专业术语要翻译 | "SWE-bench Pro 23%"→"裸跑 AI：仅 23 分"。术语加脚注说明来源，保留可信度 |
| 去专业术语不等于丢数据 | 数字保留但用人话包装，脚注补充来源说明 |
| 每页回答一个问题 | 一页只讲一个核心观点，不要试图塞太多 |

### 统一色板系统（蓝色系主题）

**适用场景**：技术分享、原理解析类演示（如 Agent Harness 这类）

**核心原则**：颜色只出现在描边、图标、小标签上。背景和卡片用中性灰白。

#### 三层蓝色色板

| 层级 | 用途 | 主色 | 渐变起始 | 渐变结束 | 适用组件 |
|------|------|------|---------|---------|---------|
| 信息层 | 上下文管理、状态记忆 | `#3b82f6` | `#dbeafe` | `#bfdbfe` | 看到什么、记住什么 |
| 执行层 | 工具系统、编排系统 | `#2563eb` | `#dbeafe` | `#bfdbfe` | 能做什么、怎么协作 |
| 保障层 | 反馈系统、沙箱、人机协作 | `#60a5fa` | `#eff6ff` | `#dbeafe` | 做错了怎么办 |

#### 中性色（背景/卡片）

| 用途 | 色值 |
|------|------|
| 卡片背景 | `#f8fafc` |
| 卡片描边 | `#e2e8f0` |
| 区块背景 | `#f1f5f9` |
| 主文字 | `#1e293b` |
| 副文字 | `#64748b` |
| 淡化文字 | `#94a3b8` |

#### 红色（仅用于错误/禁止语义）

| 用途 | 色值 |
|------|------|
| 错误/危险 | `#ef4444` |
| 错误背景 | `#fef2f2` |
| 错误描边 | `#fecaca` |

#### 使用约束

- ❌ 禁止出现绿色、橙色、紫色等其他色系（除非有明确的语义需要）
- ❌ 禁止在卡片背景上使用纯色填充 — 必须用渐变（从饱和到淡）
- ❌ 禁止使用"蓝色家族贯穿全场"等提示风格文字 — 读者不需要知道你的配色逻辑
- ✅ 渐变方向：垂直（`x1="0" y1="0" x2="0" y2="1"`），从上到下由饱和到淡

### 中文内容规范

| 规则 | 说明 | 示例 |
|------|------|------|
| 术语首次出现附英文括号 | 中文中文名 (English Term) | 上下文管理 (Context)、工具系统 (Tooling)、门控 (Gate) |
| 英文引用翻译为中文 | 翻译后保留来源署名 | "找到最小的高信号令牌集合..." — Anthropic |
| 避免"提示风格"文字 | 不要写"蓝色家族贯穿全场"等设计说明 | 直接删掉，读者不需要知道配色逻辑 |
| "反思"改为"启发" | 负面列举改为正面引导 | ~~"诚实反思：流水线静态"~~ → "💡 启发：可探索条件分支支持更灵活流程" |
| 品牌名保留英文 | 产品/公司名不翻译 | LangChain、Anthropic、Stripe、Spotify |
| 技术标签保留英文 | 代码/配置术语不翻译 | allowWriteOperations、JSON Schema、sql_validator.py |

#### 术语英文括号示例（已验证列表）

| 中文 | 英文 |
|------|------|
| 非确定性 | Non-determinism |
| 无状态 | Stateless |
| 无责任 | No Accountability |
| 约束 | Constrain |
| 记忆 | Memory |
| 兜底 | Safeguard |
| 上下文管理 | Context |
| 状态记忆 | Memory |
| 工具系统 | Tooling |
| 编排系统 | Orchestration |
| 反馈系统 | Feedback |
| 沙箱环境 | Sandbox |
| 人机协作 | Human-in-the-Loop |
| 渐进式信息展示 | Progressive Disclosure |
| 防错设计 | Poka-yoke |
| 计算型检查 | Computational |
| 推理型评审 | Inferential |
| 机械式强制 | Mechanical Enforcement |
| 串行链接 | Prompt Chaining |
| 评估-优化循环 | Evaluator-Optimizer |
| 单一职责 | Single Responsibility |
| 减法隔离 | Subtractive Isolation |
| 子代理专业化 | Sub-agent |
| 载体决定强制力 | Medium is the Enforcement |
| 门控 | Gate |

### 通用布局规则

| 规则 | 说明 | CSS 实现 |
|------|------|----------|
| 字体自适应 | 字体大小相对于 box 比例固定 | 用 `vw` 单位（1vw = 窗口宽度的 1%）|
| 标题一行 | 标题尽量一行，放不下要缩小字体 | `white-space: nowrap` 或缩小 font-size |
| 减少空白 | 不能有太多空白，内容要充实 | 减小 padding/gap，增大字体 |
| 内容不超出 | 加 overflow 控制，确保不超出视口 | `overflow: hidden` + `min-height: 0` |
| 底部居中 | 所有底部总结用 flexbox 居中 | `display: flex; align-items: center; justify-content: center;` |

### 架构图原则（SVG 优先）

**⚠️ 必须使用 inline SVG 绘制架构图，禁止使用 emoji 或 ASCII 文字图。**

| 要点 | 说明 |
|------|------|
| SVG 优先 | 所有架构图、流程图、分层图必须用 inline SVG 绘制 |
| 完整框架 | 不是简单的左右/垂直结构，要整合上层+底层 |
| 紧凑布局 | SVG viewBox 固定比例，自适应容器宽度 |
| 箭头连接 | 用 SVG `<line>` + `<marker>` 绘制箭头 |
| 分层展示 | Layer 1 → Layer 2 → Layer 3 → Layer 4，用背景色块区分 |
| 颜色分层 | 每个层级用不同渐变色（蓝→紫→绿），视觉清晰 |

### 流程图原则（SVG 优先）

**⚠️ 必须使用 inline SVG 绘制流程图。**

| 要点 | 说明 |
|------|------|
| 矩形节点 | 用 `<rect rx="8">` 绘制圆角矩形，内含 `<text>` 标签 |
| 箭头连线 | 用 `<line>` + SVG `<marker>` 定义箭头头 |
| 水平布局 | 节点左到右排列，用箭头 `→` 连接 |
| 区域划分 | 用半透明 `<rect>` 划分功能区（如"Python 区"、"AI 区"、"验证区"）|
| 渐变填充 | 用 `<linearGradient>` 定义渐变色，提升质感 |
| 虚线框 | 人工审批/确认节点用 `stroke-dasharray="4 2"` 表示 |

### SVG 架构图绘制规范

**容器 CSS**：
```css
.diagram-wrap svg {
    width: 100%;
    height: 100%;
    max-height: 100%;
}
```

**基本结构模板**：
```html
<svg viewBox="0 0 960 320" xmlns="http://www.w3.org/2000/svg"
     font-family="PingFang SC,Microsoft YaHei,sans-serif">
    <!-- 定义箭头 marker -->
    <defs>
        <marker id="arrow" markerWidth="8" markerHeight="6"
                refX="8" refY="3" orient="auto">
            <polygon points="0 0,8 3,0 6" fill="#64748b"/>
        </marker>
        <!-- 定义渐变 -->
        <linearGradient id="gBlue" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="#dbeafe"/>
            <stop offset="100%" stop-color="#bfdbfe"/>
        </linearGradient>
    </defs>
    
    <!-- 背景区域划分 -->
    <rect x="30" y="15" width="320" height="140" rx="10"
          fill="#e0f2fe" opacity=".5"/>
    <text x="190" y="35" text-anchor="middle" font-size="11"
          fill="#0369a1" font-weight="600">区域标签</text>
    
    <!-- 节点 -->
    <rect x="40" y="50" width="90" height="48" rx="8"
          fill="url(#gBlue)" stroke="#3b82f6" stroke-width="1.5"/>
    <text x="85" y="70" text-anchor="middle" font-size="11"
          font-weight="700" fill="#1e293b">节点标题</text>
    <text x="85" y="86" text-anchor="middle" font-size="9"
          fill="#64748b">副标题</text>
    
    <!-- 箭头连线 -->
    <line x1="130" y1="74" x2="148" y2="74"
          stroke="#64748b" stroke-width="1.5" marker-end="url(#arrow)"/>
</svg>
```

**常用配色方案**：
| 用途 | 边框色 | 渐变 start | 渐变 end |
|------|--------|-----------|----------|
| 信息层（上下文/记忆） | #3b82f6 | #dbeafe | #bfdbfe |
| 执行层（工具/编排） | #2563eb | #dbeafe | #bfdbfe |
| 保障层（反馈/沙箱/协作） | #60a5fa | #eff6ff | #dbeafe |
| 中性/框架 | #94a3b8 | #f8fafc | #f1f5f9 |
| 错误/禁止（仅此语义） | #ef4444 | #fef2f2 | #fecaca |
| 警告/注意 | #f59e0b | #fef3c7 | #fde68a |

**SVG 尺寸规范**：
- viewBox 宽度固定 960，高度按内容调整（320/400/500）
- 字体：标题 11-12px，副标题 9-10px
- 节点：宽度 80-100px，高度 40-55px，rx="8"
- 箭头：stroke-width 1.5，marker 8×6
- 区域背景：opacity 0.5，rx=10
- font-size 格式：**`font-size="12"` 无 px 后缀** — SVG 属性不接受 px，写 `font-size="12px"` 会被静默忽略或渲染异常

**特殊节点样式**：
- **人工确认/阻塞点**：`stroke-dasharray="4 2"` + 黄色填充 + 加粗边框
- **关键产出**：双线边框或加粗描边
- **可选/扩展**：虚线边框 + 低透明度

### 数据可视化（SVG 图表）

**简单数据对比**：用 CSS 表格 + 条形背景
```html
<div class="stat">
    <div style="font-size:2vw;font-weight:700;color:var(--primary)">7.6×</div>
    <div style="font-size:1vw;color:#64748b">上下文压缩</div>
</div>
```

**进度/百分比**：用 CSS 进度条
```html
<div style="background:#e2e8f0;border-radius:0.3vw;height:0.4vw;overflow:hidden">
    <div style="background:var(--primary);height:100%;width:99.97%"></div>
</div>
```

**需要复杂图表时**：用 inline SVG 绘制简单柱状图/饼图，或引入 Chart.js CDN

### 演示辅助系统（导航 + 演示光标）

**所有演示必须包含**：nav dots + progress bar + 聚光灯光标。

#### 导航系统 HTML 模板

```html
<!-- Progress bar (fixed top) -->
<div style="position:fixed;top:0;left:0;width:100%;height:3px;z-index:100;background:#e2e8f0;">
    <div id="progressBar" style="height:100%;width:0%;background:linear-gradient(90deg,#3b82f6,#2563eb);transition:width 0.4s ease;"></div>
</div>

<!-- Nav dots (fixed right) -->
<div style="position:fixed;right:12px;top:50%;transform:translateY(-50%);z-index:100;display:flex;flex-direction:column;gap:6px;">
    <!-- slide-0 无 nav dot，从 slide-1 开始 -->
    <div class="nav-dot" data-slide="1" title="Slide 1"></div>
    <div class="nav-dot" data-slide="2" title="Slide 2"></div>
    <!-- ... -->
</div>
```

#### 导航系统 CSS 模板

```css
.nav-dot {
    width: 6px; height: 6px; border-radius: 50%;
    background: #cbd5e1; cursor: pointer;
    transition: all 0.3s ease;
}
.nav-dot.active { background: #3b82f6; transform: scale(1.5); }
.nav-dot:hover { background: #3b82f6; }
```

#### 聚光灯光标（按 P 开关）

```html
<!-- Spotlight cursor (add after progress bar) -->
<div class="pres-cursor" id="presCursor">
    <div class="pres-spotlight"></div>
    <div class="pres-dot"></div>
</div>
<div class="pres-cursor-hint" id="presCursorHint">按 P 开关演示光标</div>
```

```css
.pres-cursor {
    position: fixed; pointer-events: none; z-index: 9999;
    width: 0; height: 0; opacity: 0; transition: opacity 0.4s ease;
}
.pres-cursor.active { opacity: 1; }
.pres-cursor.fading .pres-spotlight { opacity: 0.04; }
.pres-cursor.fading .pres-dot { opacity: 0.4; }
.pres-spotlight {
    position: absolute; width: 220px; height: 220px;
    top: 50%; left: 50%; transform: translate(-50%, -50%);
    border-radius: 50%;
    background: radial-gradient(circle, rgba(59,130,246,0.16) 0%, rgba(59,130,246,0.09) 35%, rgba(59,130,246,0.03) 60%, transparent 80%);
    opacity: 0.14; transition: opacity 0.6s ease;
}
.pres-dot {
    position: absolute; width: 8px; height: 8px;
    top: 50%; left: 50%; transform: translate(-50%, -50%);
    border-radius: 50%; background: #3b82f6;
    box-shadow: 0 0 8px rgba(59,130,246,0.5); transition: opacity 0.6s ease;
}
.pres-cursor-hint {
    position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%);
    font-size: 12px; color: #94a3b8; background: rgba(255,255,255,0.9);
    padding: 6px 16px; border-radius: 20px; border: 1px solid #e2e8f0;
    z-index: 9999; opacity: 0; transition: opacity 0.3s ease;
    pointer-events: none;
}
.pres-cursor-hint.show { opacity: 1; }
```

```javascript
// Spotlight cursor JS (add after SlidePresentation init)
(function() {
    const cursor = document.getElementById('presCursor');
    const hint = document.getElementById('presCursorHint');
    let enabled = false, mouseX = -200, mouseY = -200;
    let curX = -200, curY = -200, hideTimer = null, rafId = null;
    function showHint(text, dur) {
        hint.textContent = text; hint.classList.add('show');
        setTimeout(() => hint.classList.remove('show'), dur || 2000);
    }
    function animate() {
        curX += (mouseX - curX) * 0.25; curY += (mouseY - curY) * 0.25;
        cursor.style.left = curX + 'px'; cursor.style.top = curY + 'px';
        if (enabled) rafId = requestAnimationFrame(animate);
    }
    document.addEventListener('mousemove', (e) => {
        mouseX = e.clientX; mouseY = e.clientY;
        if (!enabled) return;
        cursor.classList.add('active'); cursor.classList.remove('fading');
        clearTimeout(hideTimer);
        hideTimer = setTimeout(() => cursor.classList.add('fading'), 2000);
    });
    document.addEventListener('keydown', (e) => {
        if ((e.key === 'p' || e.key === 'P') && e.target.tagName !== 'INPUT' && e.target.tagName !== 'TEXTAREA') {
            enabled = !enabled;
            if (enabled) {
                document.body.style.cursor = 'none';
                cursor.classList.add('active'); cursor.classList.remove('fading');
                showHint('演示光标已开启 · 按 P 关闭', 2000);
                rafId = requestAnimationFrame(animate);
            } else {
                document.body.style.cursor = '';
                cursor.classList.remove('active', 'fading');
                cancelAnimationFrame(rafId);
                showHint('演示光标已关闭', 1500);
            }
        }
    });
})();
```

### 愿景图原则

| 要点 | 说明 |
|------|------|
| 云端在上 | 云端 Agent 放顶部，有"云端感" |
| 左右结对 | 员工 ⬌ 结对 ⬌ 本地 Agent（横向布局）|
| 字体缩小 | 结对标记字体缩小一倍（0.9vw）|
| 多个成员 | 4个员工+4个Agent，表达团队协作 |

### 关键字高亮

```css
.key {
    font-weight: 700;
    color: var(--primary);
}
```

应用场景：
- 痛点的结果性结论
- 解决方案的核心价值
- 架构的特点
- 核心实践的步骤名称
- 效果数据的关键数字
- 未来演进的关键能力

### 交付规则

| 场景 | 方式 |
|------|------|
| 晚上12点前 | 直接发文件路径，老板自己打开 |
| 其他时间 | 用 message 发飞书 |

Create zero-dependency HTML presentations that run entirely in the browser. This skill helps non-designers discover their preferred aesthetic through visual exploration ("show, don't tell"), then generates production-quality slide decks.

## Core Philosophy

1. **Zero Dependencies** — Single HTML files with inline CSS/JS. No npm, no build tools.
2. **Show, Don't Tell** — People don't know what they want until they see it. Generate visual previews, not abstract choices.
3. **Distinctive Design** — Avoid generic "AI slop" aesthetics. Every presentation should feel custom-crafted.
4. **Production Quality** — Code should be well-commented, accessible, and performant.
5. **Viewport Fitting (CRITICAL)** — Every slide MUST fit exactly within the viewport. No scrolling within slides, ever. This is non-negotiable.

---

## CRITICAL: Viewport Fitting Requirements

**This section is mandatory for ALL presentations. Every slide must be fully visible without scrolling on any screen size.**

### The Golden Rule

```
Each slide = exactly one viewport height (100vh/100dvh)
Content overflows? → Split into multiple slides or reduce content
Never scroll within a slide.
```

### Content Density Limits

To guarantee viewport fitting, enforce these limits per slide:

| Slide Type | Maximum Content |
|------------|-----------------|
| Title slide | 1 heading + 1 subtitle + optional tagline |
| Content slide | 1 heading + 4-6 bullet points OR 1 heading + 2 paragraphs |
| Feature grid | 1 heading + 6 cards maximum (2x3 or 3x2 grid) |
| Code slide | 1 heading + 8-10 lines of code maximum |
| Quote slide | 1 quote (max 3 lines) + attribution |
| Image slide | 1 heading + 1 image (max 60vh height) |

**If content exceeds these limits → Split into multiple slides**

### Required CSS Architecture

Every presentation MUST include this base CSS for viewport fitting:

```css
/* ===========================================
   VIEWPORT FITTING: MANDATORY BASE STYLES
   These styles MUST be included in every presentation.
   They ensure slides fit exactly in the viewport.
   =========================================== */

/* 1. Lock html/body to viewport */
html, body {
    height: 100%;
    overflow-x: hidden;
}

html {
    scroll-snap-type: y mandatory;
    scroll-behavior: smooth;
}

/* 2. Each slide = exact viewport height */
.slide {
    width: 100vw;
    height: 100vh;
    height: 100dvh; /* Dynamic viewport height for mobile browsers */
    overflow: hidden; /* CRITICAL: Prevent ANY overflow */
    scroll-snap-align: start;
    display: flex;
    flex-direction: column;
    position: relative;
}

/* 3. Content container with flex for centering */
.slide-content {
    flex: 1;
    display: flex;
    flex-direction: column;
    justify-content: center;
    max-height: 100%;
    overflow: hidden; /* Double-protection against overflow */
    padding: var(--slide-padding);
}

/* 4. ALL typography uses clamp() for responsive scaling */
:root {
    /* Titles scale from mobile to desktop */
    --title-size: clamp(1.5rem, 5vw, 4rem);
    --h2-size: clamp(1.25rem, 3.5vw, 2.5rem);
    --h3-size: clamp(1rem, 2.5vw, 1.75rem);

    /* Body text */
    --body-size: clamp(0.75rem, 1.5vw, 1.125rem);
    --small-size: clamp(0.65rem, 1vw, 0.875rem);

    /* Spacing scales with viewport */
    --slide-padding: clamp(1rem, 4vw, 4rem);
    --content-gap: clamp(0.5rem, 2vw, 2rem);
    --element-gap: clamp(0.25rem, 1vw, 1rem);
}

/* 5. Cards/containers use viewport-relative max sizes */
.card, .container, .content-box {
    max-width: min(90vw, 1000px);
    max-height: min(80vh, 700px);
}

/* 6. Lists auto-scale with viewport */
.feature-list, .bullet-list {
    gap: clamp(0.4rem, 1vh, 1rem);
}

.feature-list li, .bullet-list li {
    font-size: var(--body-size);
    line-height: 1.4;
}

/* 7. Grids adapt to available space */
.grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 250px), 1fr));
    gap: clamp(0.5rem, 1.5vw, 1rem);
}

/* 8. Images constrained to viewport */
img, .image-container {
    max-width: 100%;
    max-height: min(50vh, 400px);
    object-fit: contain;
}

/* ===========================================
   RESPONSIVE BREAKPOINTS
   Aggressive scaling for smaller viewports
   =========================================== */

/* Short viewports (< 700px height) */
@media (max-height: 700px) {
    :root {
        --slide-padding: clamp(0.75rem, 3vw, 2rem);
        --content-gap: clamp(0.4rem, 1.5vw, 1rem);
        --title-size: clamp(1.25rem, 4.5vw, 2.5rem);
        --h2-size: clamp(1rem, 3vw, 1.75rem);
    }
}

/* Very short viewports (< 600px height) */
@media (max-height: 600px) {
    :root {
        --slide-padding: clamp(0.5rem, 2.5vw, 1.5rem);
        --content-gap: clamp(0.3rem, 1vw, 0.75rem);
        --title-size: clamp(1.1rem, 4vw, 2rem);
        --body-size: clamp(0.7rem, 1.2vw, 0.95rem);
    }

    /* Hide non-essential elements */
    .nav-dots, .keyboard-hint, .decorative {
        display: none;
    }
}

/* Extremely short (landscape phones, < 500px height) */
@media (max-height: 500px) {
    :root {
        --slide-padding: clamp(0.4rem, 2vw, 1rem);
        --title-size: clamp(1rem, 3.5vw, 1.5rem);
        --h2-size: clamp(0.9rem, 2.5vw, 1.25rem);
        --body-size: clamp(0.65rem, 1vw, 0.85rem);
    }
}

/* Narrow viewports (< 600px width) */
@media (max-width: 600px) {
    :root {
        --title-size: clamp(1.25rem, 7vw, 2.5rem);
    }

    /* Stack grids vertically */
    .grid {
        grid-template-columns: 1fr;
    }
}

/* ===========================================
   REDUCED MOTION
   Respect user preferences
   =========================================== */
@media (prefers-reduced-motion: reduce) {
    *, *::before, *::after {
        animation-duration: 0.01ms !important;
        transition-duration: 0.2s !important;
    }

    html {
        scroll-behavior: auto;
    }
}
```

### Overflow Prevention Checklist

Before generating any presentation, mentally verify:

1. ✅ Every `.slide` has `height: 100vh; height: 100dvh; overflow: hidden;`
2. ✅ All font sizes use `clamp(min, preferred, max)`
3. ✅ All spacing uses `clamp()` or viewport units
4. ✅ Content containers have `max-height` constraints
5. ✅ Images have `max-height: min(50vh, 400px)` or similar
6. ✅ Grids use `auto-fit` with `minmax()` for responsive columns
7. ✅ Breakpoints exist for heights: 700px, 600px, 500px
8. ✅ No fixed pixel heights on content elements
9. ✅ Content per slide respects density limits

### When Content Doesn't Fit

If you find yourself with too much content:

**DO:**
- Split into multiple slides
- Reduce bullet points (max 5-6 per slide)
- Shorten text (aim for 1-2 lines per bullet)
- Use smaller code snippets
- Create a "continued" slide

**DON'T:**
- Reduce font size below readable limits
- Remove padding/spacing entirely
- Allow any scrolling
- Cram content to fit

### Testing Viewport Fit

After generating, recommend the user test at these sizes:
- Desktop: 1920×1080, 1440×900, 1280×720
- Tablet: 1024×768, 768×1024 (portrait)
- Mobile: 375×667, 414×896
- Landscape phone: 667×375, 896×414

---

## Phase 0: Detect Mode

First, determine what the user wants:

**Mode A: New Presentation**
- User wants to create slides from scratch
- Proceed to Phase 1 (大纲编写)

**Mode B: PPT Conversion**
- User has a PowerPoint file (.ppt, .pptx) to convert
- Proceed to Phase 6 (PPT Extraction)

**Mode C: Existing Presentation Enhancement**
- User has an HTML presentation and wants to improve it
- Read the existing file, understand the structure, then enhance

---

## Phase 1: 大纲编写（Outline）

### 目标
从用户需求生成结构化大纲，经用户确认后作为后续所有阶段的输入。

### 大纲格式规范（outline.md）

每页包含以下字段：

```markdown
## Slide N: [页面标题]

### 元信息
- 色彩层: 信息层 / 执行层 / 保障层
- 布局类型: content-slide / ctx-slide / end-slide
- 副标题/引用: （如有）

### 布局结构
| 区域 | 占比 | 内容描述 |
|------|------|---------|
| Header | X% | 标题 + 引用 |
| Body | Y% | SVG 图表 / 卡片 / 对比 |
| Footer | Z% | 实践卡片 / 启发 |

### 关键元素
- [ ] 元素1: （如"Softmax 柱状图：3 高蓝色柱 + 20 矮灰色柱"）
- [ ] 元素2: （如"载体决定强制力条：纯文本→Markdown→JSON"）
- [ ] 元素3: （如"3 张实践卡片"）

### 文本内容（完整）
- 标题: "..."
- 副标题: "..."
- 卡片1标题: "...", 内容: "..."
```

### 大纲确认点
- ⏸️ **必须等待用户确认**后才能进入 Phase 2
- 确认重点：页面顺序、每页核心论点、布局比例是否合理
- 用户可能在此阶段调整页面数量、合并/拆分页

---

## Phase 2: 内容&视觉指导文档（Design Spec）

### 目标
将大纲细化为精确的 SVG 参数和完整文本，让 AI 可以一次性输出高质量页面。

### Design Spec 格式规范（design-spec.md）

在 outline.md 基础上，每页增加以下详细参数：

```markdown
## Slide N: [页面标题]（细化）

### 元信息（同大纲）

### SVG 关键参数
- 左图: viewBox="0 0 WxH"
  - 柱状图: 3 蓝 (x=40,86,132 w=32 h=80) + 20 灰 (w=16 h=18)
  - 标签: "充足注意力" (x=310 y=112) / "注意力被稀释" (x=310 y=228)
- 右图: viewBox="0 0 WxH"
  - U 型: 9 段渐变柱 (h: 90%,75%,55%,35%,15%,35%,55%,75%,90%)
  - 红色标注: "中间" (x=480 fill="#ef4444")

### 完整文本（精确到每个 label）
| 位置 | 文本 | 字号 | 颜色 |
|------|------|------|------|
| Header 标题 | "上下文管理 (Context)：AI 此刻该看到什么" | h2 | #1e293b |
| Header 引用 | "找到最小的高信号令牌集合..." | quote | — |
| 左图标题 | "Softmax 注意力分配 (Attention Allocation)" | 15 bold | #1e293b |
| 左图标签 | "高信号 Token（少量）" | 12 600 | #3b82f6 |
| ... | ... | ... | ... |
```

### Spec 确认点
- ⏸️ **必须等待用户确认**后才能进入 Phase 4（代码生成）
- 确认重点：SVG 参数是否合理、文本是否完整、色彩层是否正确

---

## Phase 3: 风格选择（Style Discovery）

**CRITICAL: This is the "show, don't tell" phase.**

Most people can't articulate design preferences in words. Instead of asking "do you want minimalist or bold?", we generate mini-previews and let them react.

### How Users Choose Presets

Users can select a style in **two ways**:

**Option A: Guided Discovery (Default)**
- User answers mood questions
- Skill generates 3 preview files based on their answers
- User views previews in browser and picks their favorite
- This is best for users who don't have a specific style in mind

**Option B: Direct Selection**
- If user already knows what they want, they can request a preset by name
- Example: "Use the Bold Signal style" or "I want something like Dark Botanical"
- Skip to Phase 4 immediately

**Available Presets:**
| Preset | Vibe | Best For |
|--------|------|----------|
| Bold Signal | Confident, high-impact | Pitch decks, keynotes |
| Electric Studio | Clean, professional | Agency presentations |
| Creative Voltage | Energetic, retro-modern | Creative pitches |
| Dark Botanical | Elegant, sophisticated | Premium brands |
| Notebook Tabs | Editorial, organized | Reports, reviews |
| Pastel Geometry | Friendly, approachable | Product overviews |
| Split Pastel | Playful, modern | Creative agencies |
| Vintage Editorial | Witty, personality-driven | Personal brands |
| Neon Cyber | Futuristic, techy | Tech startups |
| Terminal Green | Developer-focused | Dev tools, APIs |
| Swiss Modern | Minimal, precise | Corporate, data |
| Paper & Ink | Literary, thoughtful | Storytelling |

### Step 2.0: Style Path Selection

First, ask how the user wants to choose their style:

**Question: Style Selection Method**
- Header: "Style"
- Question: "How would you like to choose your presentation style?"
- Options:
  - "Show me options" — Generate 3 previews based on my needs (recommended for most users)
  - "I know what I want" — Let me pick from the preset list directly

**If "Show me options"** → Continue to Step 2.1 (Mood Selection)

**If "I know what I want"** → Show preset picker:

**Question: Pick a Preset**
- Header: "Preset"
- Question: "Which style would you like to use?"
- Options:
  - "Bold Signal" — Vibrant card on dark, confident and high-impact
  - "Dark Botanical" — Elegant dark with soft abstract shapes
  - "Notebook Tabs" — Editorial paper look with colorful section tabs
  - "Pastel Geometry" — Friendly pastels with decorative pills

(If user picks one, skip to Phase 4. If they want to see more options, show additional presets or proceed to guided discovery.)

### Step 2.1: Mood Selection (Guided Discovery)

**Question 1: Feeling**
- Header: "Vibe"
- Question: "What feeling should the audience have when viewing your slides?"
- Options:
  - "Impressed/Confident" — Professional, trustworthy, this team knows what they're doing
  - "Excited/Energized" — Innovative, bold, this is the future
  - "Calm/Focused" — Clear, thoughtful, easy to follow
  - "Inspired/Moved" — Emotional, storytelling, memorable
- multiSelect: true (can choose up to 2)

### Step 2.2: Generate Style Previews

Based on their mood selection, generate **3 distinct style previews** as mini HTML files in a temporary directory. Each preview should be a single title slide showing:

- Typography (font choices, heading/body hierarchy)
- Color palette (background, accent, text colors)
- Animation style (how elements enter)
- Overall aesthetic feel

**Preview Styles to Consider (pick 3 based on mood):**

| Mood | Style Options |
|------|---------------|
| Impressed/Confident | "Bold Signal", "Electric Studio", "Dark Botanical" |
| Excited/Energized | "Creative Voltage", "Neon Cyber", "Split Pastel" |
| Calm/Focused | "Notebook Tabs", "Paper & Ink", "Swiss Modern" |
| Inspired/Moved | "Dark Botanical", "Vintage Editorial", "Pastel Geometry" |

**IMPORTANT: Never use these generic patterns:**
- Purple gradients on white backgrounds
- Inter, Roboto, or system fonts
- Standard blue primary colors
- Predictable hero layouts

**Instead, use distinctive choices:**
- Unique font pairings (Clash Display, Satoshi, Cormorant Garamond, DM Sans, etc.)
- Cohesive color themes with personality
- Atmospheric backgrounds (gradients, subtle patterns, depth)
- Signature animation moments

### Step 2.3: Present Previews

Create the previews in: `.claude-design/slide-previews/`

```
.claude-design/slide-previews/
├── style-a.html   # First style option
├── style-b.html   # Second style option
├── style-c.html   # Third style option
└── assets/        # Any shared assets
```

Each preview file should be:
- Self-contained (inline CSS/JS)
- A single "title slide" showing the aesthetic
- Animated to demonstrate motion style
- ~50-100 lines, not a full presentation

Present to user:
```
I've created 3 style previews for you to compare:

**Style A: [Name]** — [1 sentence description]
**Style B: [Name]** — [1 sentence description]
**Style C: [Name]** — [1 sentence description]

Open each file to see them in action:
- .claude-design/slide-previews/style-a.html
- .claude-design/slide-previews/style-b.html
- .claude-design/slide-previews/style-c.html

Take a look and tell me:
1. Which style resonates most?
2. What do you like about it?
3. Anything you'd change?
```

Then use AskUserQuestion:

**Question: Pick Your Style**
- Header: "Style"
- Question: "Which style preview do you prefer?"
- Options:
  - "Style A: [Name]" — [Brief description]
  - "Style B: [Name]" — [Brief description]
  - "Style C: [Name]" — [Brief description]
  - "Mix elements" — Combine aspects from different styles

If "Mix elements", ask for specifics.

---

## Phase 4: 代码生成（Code Generation）

Now generate the full presentation based on:
- Content from Phase 1
- Style from Phase 2

### File Structure

For single presentations:
```
presentation.html    # Self-contained presentation
assets/              # Images, if any
```

For projects with multiple presentations:
```
[presentation-name].html
[presentation-name]-assets/
```

### HTML Architecture

Follow this structure for all presentations:

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Presentation Title</title>

    <!-- Fonts (use Fontshare or Google Fonts) -->
    <link rel="stylesheet" href="https://api.fontshare.com/v2/css?f[]=...">

    <style>
        /* ===========================================
           CSS CUSTOM PROPERTIES (THEME)
           Easy to modify: change these to change the whole look
           =========================================== */
        :root {
            /* Colors */
            --bg-primary: #0a0f1c;
            --bg-secondary: #111827;
            --text-primary: #ffffff;
            --text-secondary: #9ca3af;
            --accent: #00ffcc;
            --accent-glow: rgba(0, 255, 204, 0.3);

            /* Typography - MUST use clamp() for responsive scaling */
            --font-display: 'Clash Display', sans-serif;
            --font-body: 'Satoshi', sans-serif;
            --title-size: clamp(2rem, 6vw, 5rem);
            --subtitle-size: clamp(0.875rem, 2vw, 1.25rem);
            --body-size: clamp(0.75rem, 1.2vw, 1rem);

            /* Spacing - MUST use clamp() for responsive scaling */
            --slide-padding: clamp(1.5rem, 4vw, 4rem);
            --content-gap: clamp(1rem, 2vw, 2rem);

            /* Animation */
            --ease-out-expo: cubic-bezier(0.16, 1, 0.3, 1);
            --duration-normal: 0.6s;
        }

        /* ===========================================
           BASE STYLES
           =========================================== */
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        html {
            scroll-behavior: smooth;
            scroll-snap-type: y mandatory;
            height: 100%;
        }

        body {
            font-family: var(--font-body);
            background: var(--bg-primary);
            color: var(--text-primary);
            overflow-x: hidden;
            height: 100%;
        }

        /* ===========================================
           SLIDE CONTAINER
           CRITICAL: Each slide MUST fit exactly in viewport
           - Use height: 100vh (NOT min-height)
           - Use overflow: hidden to prevent scroll
           - Content must scale with clamp() values
           =========================================== */
        .slide {
            width: 100vw;
            height: 100vh; /* EXACT viewport height - no scrolling */
            height: 100dvh; /* Dynamic viewport height for mobile */
            padding: var(--slide-padding);
            scroll-snap-align: start;
            display: flex;
            flex-direction: column;
            justify-content: center;
            position: relative;
            overflow: hidden; /* Prevent any content overflow */
        }

        /* Content wrapper that prevents overflow */
        .slide-content {
            flex: 1;
            display: flex;
            flex-direction: column;
            justify-content: center;
            max-height: 100%;
            overflow: hidden;
        }

        /* ===========================================
           RESPONSIVE BREAKPOINTS
           Adjust content for different screen sizes
           =========================================== */
        @media (max-height: 600px) {
            :root {
                --slide-padding: clamp(1rem, 3vw, 2rem);
                --content-gap: clamp(0.5rem, 1.5vw, 1rem);
            }
        }

        @media (max-width: 768px) {
            :root {
                --title-size: clamp(1.5rem, 8vw, 3rem);
            }
        }

        @media (max-height: 500px) and (orientation: landscape) {
            /* Extra compact for landscape phones */
            :root {
                --title-size: clamp(1.25rem, 5vw, 2rem);
                --slide-padding: clamp(0.75rem, 2vw, 1.5rem);
            }
        }

        /* ===========================================
           ANIMATIONS
           Trigger via .visible class (added by JS on scroll)
           =========================================== */
        .reveal {
            opacity: 0;
            transform: translateY(30px);
            transition: opacity var(--duration-normal) var(--ease-out-expo),
                        transform var(--duration-normal) var(--ease-out-expo);
        }

        .slide.visible .reveal {
            opacity: 1;
            transform: translateY(0);
        }

        /* Stagger children */
        .reveal:nth-child(1) { transition-delay: 0.1s; }
        .reveal:nth-child(2) { transition-delay: 0.2s; }
        .reveal:nth-child(3) { transition-delay: 0.3s; }
        .reveal:nth-child(4) { transition-delay: 0.4s; }

        /* ... more styles ... */
    </style>
</head>
<body>
    <!-- Progress bar (optional) -->
    <div class="progress-bar"></div>

    <!-- Navigation dots (optional) -->
    <nav class="nav-dots">
        <!-- Generated by JS -->
    </nav>

    <!-- Slides -->
    <section class="slide title-slide">
        <h1 class="reveal">Presentation Title</h1>
        <p class="reveal">Subtitle or author</p>
    </section>

    <section class="slide">
        <h2 class="reveal">Slide Title</h2>
        <p class="reveal">Content...</p>
    </section>

    <!-- More slides... -->

    <script>
        /* ===========================================
           SLIDE PRESENTATION CONTROLLER
           Handles navigation, animations, and interactions
           =========================================== */

        class SlidePresentation {
            constructor() {
                // ... initialization
            }

            // ... methods
        }

        // Initialize
        new SlidePresentation();
    </script>
</body>
</html>
```

### Required JavaScript Features

Every presentation should include:

1. **SlidePresentation Class** — Main controller
   - Keyboard navigation (arrows, space)
   - Touch/swipe support
   - Mouse wheel navigation
   - Progress bar updates
   - Navigation dots

2. **Intersection Observer** — For scroll-triggered animations
   - Add `.visible` class when slides enter viewport
   - Trigger CSS animations efficiently

3. **Optional Enhancements** (based on style):
   - Custom cursor with trail
   - Particle system background (canvas)
   - Parallax effects
   - 3D tilt on hover
   - Magnetic buttons
   - Counter animations

### Code Quality Requirements

**Comments:**
Every section should have clear comments explaining:
- What it does
- Why it exists
- How to modify it

```javascript
/* ===========================================
   CUSTOM CURSOR
   Creates a stylized cursor that follows mouse with a trail effect.
   - Uses lerp (linear interpolation) for smooth movement
   - Grows larger when hovering over interactive elements
   =========================================== */
class CustomCursor {
    constructor() {
        // ...
    }
}
```

**Accessibility:**
- Semantic HTML (`<section>`, `<nav>`, `<main>`)
- Keyboard navigation works
- ARIA labels where needed
- Reduced motion support

```css
@media (prefers-reduced-motion: reduce) {
    .reveal {
        transition: opacity 0.3s ease;
        transform: none;
    }
}
```

**CSS Function Negation:**
- Never negate CSS functions directly — `-clamp()`, `-min()`, `-max()` are silently ignored by browsers with no console error
- Always use `calc(-1 * clamp(...))` instead. See STYLE_PRESETS.md → "CSS Gotchas" for details.

**Responsive & Viewport Fitting (CRITICAL):**

**See the "CRITICAL: Viewport Fitting Requirements" section above for complete CSS and guidelines.**

Quick reference:
- Every `.slide` must have `height: 100vh; height: 100dvh; overflow: hidden;`
- All typography and spacing must use `clamp()`
- Respect content density limits (max 4-6 bullets, max 6 cards, etc.)
- Include breakpoints for heights: 700px, 600px, 500px
- When content doesn't fit → split into multiple slides, never scroll

### 分段并行策略

| 页数 | 策略 | 说明 |
|------|------|------|
| ≤10 | 单文件 | 一次生成完整 HTML |
| 11-15 | 拆 2 段 | p1 (slides 0-N) + p2 (slides N+1-M) |
| >15 | 拆 3 段 | p1 + p2 + p3，每段 5-7 页 |

#### 合并规则（关键！）

1. **CSS**: 只保留一份 `:root` + base styles，去重
2. **SVG defs**: 合并所有 `<defs>`（gradient/marker id 不能冲突）
3. **Slide ID 重编号**: 必须从后往前（`slide-18→19, slide-17→18...`），正向会碰撞
4. **Nav dots**: slide-0 无 dot，从 slide-1 开始，总数 = slides - 1
5. **JS**: 只保留一个 `SlidePresentation` 类，`totalSlides = slides.length`（动态计算）

---

## Phase 5: 迭代打磨（Refinement）

### 迭代检查清单（每次修改后必须逐项检查）

#### 布局检查
- [ ] SVG viewBox 高度是否 ≥ 内容最大 y 坐标 + padding
- [ ] 标签/文字是否重叠（检查相近 y 坐标的元素，间隔需 ≥ 14px）
- [ ] 卡片/容器是否精确包裹内容（无过大空白、无内容溢出）
- [ ] 来源/脚注卡片是否占空间过大（考虑移入 SVG 内做脚注）

#### 文本检查
- [ ] 所有中文术语首次出现是否有英文括号
- [ ] 英文引用是否已翻译为中文
- [ ] 是否有"蓝色家族贯穿全场"等提示风格文字（需删除）
- [ ] "诚实反思"是否已改为"💡 启发"
- [ ] 品牌名是否保留英文

#### 色彩检查
- [ ] 是否出现了非蓝色系颜色（绿/橙/紫）— 除非有明确语义
- [ ] 卡片背景是否为中性灰 `#f8fafc`（不是彩色填充）
- [ ] 渐变是否为垂直方向（x1=0 y1=0 x2=0 y2=1）

#### 导航检查
- [ ] slide ID 是否连续（0, 1, 2, ...）
- [ ] nav dot 数量是否 = slide 数量 - 1
- [ ] 进度条是否正常工作
- [ ] 聚光灯光标按 P 是否正常开关

#### 字号检查
- [ ] SVG font-size 格式是否为 `font-size="N"`（无 px 后缀）
- [ ] 正文是否 ≥ 12px，标题是否 ≥ 14px
- [ ] CSS 变量是否已同步放大

---

## Phase 6: PPT Conversion

When converting PowerPoint files:

### Step 6.1: Extract Content

Use Python with `python-pptx` to extract:

```python
from pptx import Presentation
from pptx.util import Inches, Pt
import json
import os
import base64

def extract_pptx(file_path, output_dir):
    """
    Extract all content from a PowerPoint file.
    Returns a JSON structure with slides, text, and images.
    """
    prs = Presentation(file_path)
    slides_data = []

    # Create assets directory
    assets_dir = os.path.join(output_dir, 'assets')
    os.makedirs(assets_dir, exist_ok=True)

    for slide_num, slide in enumerate(prs.slides):
        slide_data = {
            'number': slide_num + 1,
            'title': '',
            'content': [],
            'images': [],
            'notes': ''
        }

        for shape in slide.shapes:
            # Extract title
            if shape.has_text_frame:
                if shape == slide.shapes.title:
                    slide_data['title'] = shape.text
                else:
                    slide_data['content'].append({
                        'type': 'text',
                        'content': shape.text
                    })

            # Extract images
            if shape.shape_type == 13:  # Picture
                image = shape.image
                image_bytes = image.blob
                image_ext = image.ext
                image_name = f"slide{slide_num + 1}_img{len(slide_data['images']) + 1}.{image_ext}"
                image_path = os.path.join(assets_dir, image_name)

                with open(image_path, 'wb') as f:
                    f.write(image_bytes)

                slide_data['images'].append({
                    'path': f"assets/{image_name}",
                    'width': shape.width,
                    'height': shape.height
                })

        # Extract notes
        if slide.has_notes_slide:
            notes_frame = slide.notes_slide.notes_text_frame
            slide_data['notes'] = notes_frame.text

        slides_data.append(slide_data)

    return slides_data
```

### Step 6.2: Confirm Content Structure

Present the extracted content to the user:

```
I've extracted the following from your PowerPoint:

**Slide 1: [Title]**
- [Content summary]
- Images: [count]

**Slide 2: [Title]**
- [Content summary]
- Images: [count]

...

All images have been saved to the assets folder.

Does this look correct? Should I proceed with style selection?
```

### Step 6.3: Style Selection

Proceed to Phase 3 (Style Discovery) with the extracted content in mind.

### Step 6.4: Generate HTML

Convert the extracted content into the chosen style, preserving:
- All text content
- All images (referenced from assets folder)
- Slide order
- Any speaker notes (as HTML comments or separate file)

---

## Phase 7: 交付（Delivery）

### Final Output

When the presentation is complete:

1. **Clean up temporary files**
   - Delete `.claude-design/slide-previews/` if it exists

2. **Open the presentation**
   - Use `open [filename].html` to launch in browser

3. **Provide summary**
```
Your presentation is ready!

📁 File: [filename].html
🎨 Style: [Style Name]
📊 Slides: [count]

**Navigation:**
- Arrow keys (← →) or Space to navigate
- Scroll/swipe also works
- Click the dots on the right to jump to a slide

**To customize:**
- Colors: Look for `:root` CSS variables at the top
- Fonts: Change the Fontshare/Google Fonts link
- Animations: Modify `.reveal` class timings

Would you like me to make any adjustments?
```

---

## Style Reference: Effect → Feeling Mapping

Use this guide to match animations to intended feelings:

### Dramatic / Cinematic
- Slow fade-ins (1-1.5s)
- Large scale transitions (0.9 → 1)
- Dark backgrounds with spotlight effects
- Parallax scrolling
- Full-bleed images

### Techy / Futuristic
- Neon glow effects (box-shadow with accent color)
- Particle systems (canvas background)
- Grid patterns
- Monospace fonts for accents
- Glitch or scramble text effects
- Cyan, magenta, electric blue palette

### Playful / Friendly
- Bouncy easing (spring physics)
- Rounded corners (large radius)
- Pastel or bright colors
- Floating/bobbing animations
- Hand-drawn or illustrated elements

### Professional / Corporate
- Subtle, fast animations (200-300ms)
- Clean sans-serif fonts
- Navy, slate, or charcoal backgrounds
- Precise spacing and alignment
- Minimal decorative elements
- Data visualization focus

### Calm / Minimal
- Very slow, subtle motion
- High whitespace
- Muted color palette
- Serif typography
- Generous padding
- Content-focused, no distractions

### Editorial / Magazine
- Strong typography hierarchy
- Pull quotes and callouts
- Image-text interplay
- Grid-breaking layouts
- Serif headlines, sans-serif body
- Black and white with one accent

---

## Animation Patterns Reference

### Entrance Animations

```css
/* Fade + Slide Up (most common) */
.reveal {
    opacity: 0;
    transform: translateY(30px);
    transition: opacity 0.6s var(--ease-out-expo),
                transform 0.6s var(--ease-out-expo);
}

.visible .reveal {
    opacity: 1;
    transform: translateY(0);
}

/* Scale In */
.reveal-scale {
    opacity: 0;
    transform: scale(0.9);
    transition: opacity 0.6s, transform 0.6s var(--ease-out-expo);
}

/* Slide from Left */
.reveal-left {
    opacity: 0;
    transform: translateX(-50px);
    transition: opacity 0.6s, transform 0.6s var(--ease-out-expo);
}

/* Blur In */
.reveal-blur {
    opacity: 0;
    filter: blur(10px);
    transition: opacity 0.8s, filter 0.8s var(--ease-out-expo);
}
```

### Background Effects

```css
/* Gradient Mesh */
.gradient-bg {
    background:
        radial-gradient(ellipse at 20% 80%, rgba(120, 0, 255, 0.3) 0%, transparent 50%),
        radial-gradient(ellipse at 80% 20%, rgba(0, 255, 200, 0.2) 0%, transparent 50%),
        var(--bg-primary);
}

/* Noise Texture */
.noise-bg {
    background-image: url("data:image/svg+xml,..."); /* Inline SVG noise */
}

/* Grid Pattern */
.grid-bg {
    background-image:
        linear-gradient(rgba(255,255,255,0.03) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255,255,255,0.03) 1px, transparent 1px);
    background-size: 50px 50px;
}
```

### Interactive Effects

```javascript
/* 3D Tilt on Hover */
class TiltEffect {
    constructor(element) {
        this.element = element;
        this.element.style.transformStyle = 'preserve-3d';
        this.element.style.perspective = '1000px';
        this.bindEvents();
    }

    bindEvents() {
        this.element.addEventListener('mousemove', (e) => {
            const rect = this.element.getBoundingClientRect();
            const x = (e.clientX - rect.left) / rect.width - 0.5;
            const y = (e.clientY - rect.top) / rect.height - 0.5;

            this.element.style.transform = `
                rotateY(${x * 10}deg)
                rotateX(${-y * 10}deg)
            `;
        });

        this.element.addEventListener('mouseleave', () => {
            this.element.style.transform = 'rotateY(0) rotateX(0)';
        });
    }
}
```

---

## Troubleshooting

### Common Issues

**Fonts not loading:**
- Check Fontshare/Google Fonts URL
- Ensure font names match in CSS

**Animations not triggering:**
- Verify Intersection Observer is running
- Check that `.visible` class is being added

**Scroll snap not working:**
- Ensure `scroll-snap-type` on html/body
- Each slide needs `scroll-snap-align: start`

**Mobile issues:**
- Disable heavy effects at 768px breakpoint
- Test touch events
- Reduce particle count or disable canvas

**Performance issues:**
- Use `will-change` sparingly
- Prefer `transform` and `opacity` animations
- Throttle scroll/mousemove handlers

---

## Related Skills

- **learn** — Generate FORZARA.md documentation for the presentation
- **frontend-design** — For more complex interactive pages beyond slides
- **design-and-refine:design-lab** — For iterating on component designs

---

## Example Session Flow

1. User: "I want to create a pitch deck for my AI startup"
2. Skill asks about purpose, length, content
3. User shares their bullet points and key messages
4. Skill asks about desired feeling (Impressed + Excited)
5. Skill generates 3 style previews
6. User picks Style B (Neon Cyber), asks for darker background
7. Skill generates full presentation with all slides
8. Skill opens the presentation in browser
9. User requests tweaks to specific slides
10. Final presentation delivered

---

## Conversion Session Flow

1. User: "Convert my slides.pptx to a web presentation"
2. Skill extracts content and images from PPT
3. Skill confirms extracted content with user
4. Skill asks about desired feeling/style
5. Skill generates style previews
6. User picks a style
7. Skill generates HTML presentation with preserved assets
8. Final presentation delivered
