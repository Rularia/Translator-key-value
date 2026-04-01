# Game Text Translator Tool for Web LLM Workflow / 游戏文本网页翻译回填工具

A desktop tool for game text and i18n localization, designed to work smoothly with Gemini / GPT style web translation workflows, with API support as an optional path.

一个面向游戏文本与 I18n 本地化的桌面工具，适合配合 Gemini / GPT 网页版翻译流程使用，支持 API，但不依赖 API。

## Overview / 项目说明

English:
- Supports multiple source modes: JSON key-to-value, JSON value-to-value, `left=right` text, and XML string values.
- Provides manual editing, batch editing, and auto translation workflows.
- Uses anchored blocks in Auto mode to reduce missing lines, merge errors, and write-back mismatches.
- Preserves structure and writes translations back only to target text fields.

中文：
- 支持多种输入模式：JSON 的 key->value、JSON 的 value->value、`left=right` 文本、XML 字符串节点。
- 提供手动编辑、批量编辑、自动翻译三种工作流。
- Auto 模式使用锚点编号块，尽量避免漏行、合并错位和回填错位。
- 只回写目标文本内容，不破坏原始结构。

## Advantages / 优势

English:
- Web LLM workflow is supported, so translation is not limited to paid APIs.
- You can use Gemini / GPT style web pages by copying anchored text blocks and pasting results back.
- API mode is optional, not required.
- The tool focuses on safe write-back, batch organization, and placeholder protection.

中文：
- 支持网页大模型工作流，不依赖 API 才能使用。
- 可以直接复制带锚点的文本块到 Gemini / GPT 网页版，再把结果粘贴回工具。
- API 只是可选项，不是必须项。
- 工具重点在于安全回填、批量整理和占位符保护。

## Main Features / 主要功能

English:
- Worklist / Changed / Skipped style text management
- Manual editor for single-row review
- Batch paste, find, replace, skip, and group tools
- Auto workflow for web LLMs and OpenAI-compatible APIs
- Per-source autosave and project file restore
- Multi-file batch loading for files with the same mode
- Placeholder protection for common control codes and tags

中文：
- 支持待处理 / 已修改 / 跳过等文本管理方式
- 支持逐条人工校对与编辑
- 支持批量粘贴、查找替换、跳过、分组等操作
- 支持网页大模型流程与兼容 OpenAI 的 API 流程
- 支持按来源文件分别自动保存与工程恢复
- 支持同模式多文件批量导入
- 支持常见控制符、标签、占位符保护

## Run / 运行方式

```bat
conda activate <your-conda-env>
cd /d <project-folder>
pip install -e .
set PYTHONPATH=src
python app.py
```

## Usage Notes / 使用说明

English:
- `Open Source`: load one source file directly.
- `Load Batch`: load multiple files with the same detected mode.
- `Save Output`: write translations back to the original structure.
- `Save Project`: save the current workspace state.
- `Auto`: use numbered anchor blocks for web or API translation.

中文：
- `Open Source`：直接导入单个源文件。
- `Load Batch`：导入多个同模式文件进行集中处理。
- `Save Output`：将译文按原结构回写导出。
- `Save Project`：保存当前工作区状态。
- `Auto`：通过带锚点编号的文本块进行网页或 API 翻译。

## Release / 发布说明

English:
- Windows executable builds will be uploaded to GitHub Releases.

中文：
- Windows 的 exe 版本会上传到 GitHub Releases。
