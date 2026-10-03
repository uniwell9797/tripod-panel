# 贡献指南 · Contributing

> 这个软件是**脚手架**：它把"怎么搭"写好了，但没规定只能搭成什么样。
> 你用的时候遇到"要是有这个功能就好了"，欢迎把它加回来。
>
> This project is **scaffolding**: it pins down *how to build*, not *what* to build. When you hit a
> "I wish it could do X" moment, bring X back here.

---

## 可以贡献什么 · What to contribute

| 类型 | 例子 |
| --- | --- |
| **你搭好的流程/框架** | 一套完整工作流（例如：10 套衣服批量换装 → 出图 → 配文案 → 做视频）；一种新的合成/排版方式 |
| **更好的提示词模板 / 参数默认值** | 某个功能在你的题材上效果更好 —— 把那套提示词、负向词、步数/CFG/档位提上来 |
| **新模型 / 新档位支持** | 新发布的模型、更省显存的量化档、新的加载器 |
| **把"神秘失败"变成一句人话** | 你踩的坑 + 真实原因 + 一句中文提示（这类贡献价值最高）|
| **新页面 / 新功能** | 照着现成页面抄一遍即可 —— 每个功能都是一份样板 |
| **文档 / 翻译 / 截图 / 更好的默认值** | 都算 |

**不写代码也能贡献**：提 Issue 写清你想要的流程、贴一张效果图、分享你常用的提示词与参数组合、
或者把别人会踩的坑记下来 —— 这些都会被并进文档和默认值里。

---

## 怎么提 · How to submit

```text
1. Fork 本仓库，改 panel.py 和/或 new_ui.html（就这两个文件，一般不用动别的）
2. 起一个独立实例自测（不要动你正在用的那个）：
      Windows PowerShell:
        $env:PANEL_PORT='8911'; python panel.py
      然后打开 http://127.0.0.1:8911/new 确认 200，并把你要改的那个功能真跑一遍
3. 提 PR，说清三件事：改了什么 / 为什么改 / 你怎么验证的
```

> 提示：README 里那段「交给 AI Agent 安装」的提示词，也可以直接丢给你的 Agent，
> 让它帮你**起独立实例 + 真跑一遍 + 读日志排错**，再提 PR。

---

## 几条约定 · Ground rules

1. **不引入框架、不引入构建步骤**：后端保持一个 `panel.py`，前端保持一个 `new_ui.html`，改完刷新即生效；
2. **报错必须说人话**（中文优先）：能说"哪儿不对、怎么办"，就别甩一屏 traceback；
3. **涉及显存/GPU 的改动请注明测过的显存**（例如：10GB 卡 + INT8 实测通过）；
4. **不要提交模型权重**：本仓库不带权重，只给官方下载链接（见 `models.md`）；
5. **不要提交私人图片、私人路径、任何密钥**：`.gitignore` 已经挡了一层，提交前自己再看一眼 `git status`；
6. **优先 Windows，但跨平台更好**；
7. **一个 PR 只做一件事**，方便回滚；
8. 改完请顺手更新文档：功能清单在 `README.md` / `README.en.md`，模型相关在 `models.md` / `models.en.md`。

---

## 提交前自查 · Pre-PR checklist

```text
[ ] git status 里没有私人图片 / 私人路径 / panel_config.json / 模型文件
[ ] 改动的功能在独立实例上真跑通过（写清测过的显存与模型档位）
[ ] 报错文案是中文人话，不是 traceback
[ ] 新增功能已写进 README 的功能清单（中英各一处）
[ ] 没有引入新的第三方依赖（要引入就更要说明理由）
```

---

## English summary

- Contributions welcome: pipelines you built, better prompt templates, new model/tier support,
  turning cryptic failures into one plain sentence, new pages (copy an existing one), docs/translations.
- Flow: fork → edit `panel.py` / `new_ui.html` → self-test on a separate instance
  (`PANEL_PORT=8911 python panel.py`) → PR describing what/why/how you verified.
- Rules: no frameworks or build steps; plain-language errors; state the VRAM you tested on;
  never commit model weights, private images, private paths or credentials; one change per PR;
  update the feature list in both READMEs.
