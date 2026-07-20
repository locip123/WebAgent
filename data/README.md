<h1 align="center">🌐 WebRetriever：用于高效网页智能体评估的大规模综合基准</h1>

<p align="center"><b>ECCV 2026</b></p>

<p align="center">
  <a href="https://arxiv.org/abs/2607.06118">📃 论文</a> •
  <a href="https://mininglamp-ai.github.io/WebRetriever/">🏆 排行榜</a> •
  <a href="https://github.com/Mininglamp-AI/WebRetriever">💻 代码</a>
</p>

---

## 概述

现有网页智能体评估存在三项关键局限：（1）基准覆盖不足——离线基准缺乏真实世界保真度，而在线基准在网站规模、领域多样性和意图类型方面仍受限制，导致评估结果存在偏差且过于乐观；（2）评估方法不可扩展——人工标注成本高昂，难以大规模实施，现有自动化方法对复杂查询的准确性也不足；（3）评估维度狭窄——现有协议无法系统评估智能体利用外部知识，以及执行真实部署所需端到端工作流的能力。WebRetriever 通过大规模、多样化的基准测试，可靠的自动化评估方法，以及面向部署的评估协议，弥补了这三方面的不足。


## 数据集

### 与现有基准的比较

<div>
<table style="font-size: 0.9em; text-align: center; border-collapse: collapse;">
  <thead>
    <tr>
      <th style="padding: 12px 14px; text-align: center;">意图类型</th>
      <th style="padding: 12px 14px; text-align: center;">基准</th>
      <th style="padding: 12px 14px; text-align: center;">环境</th>
      <th style="padding: 12px 14px; text-align: center;">在线</th>
      <th style="padding: 12px 14px; text-align: center;">可交互</th>
      <th style="padding: 12px 14px; text-align: right;">网站数</th>
      <th style="padding: 12px 14px; text-align: right;">任务数</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td rowspan="13" style="vertical-align: middle; text-align: center;"><b>通用</b></td>
      <td style="text-align: center; font-weight: 600;">MiniWoB</td><td style="text-align: center;">合成</td><td style="text-align: center;">❌</td><td style="text-align: center;">✔️</td><td style="text-align: right;">100</td><td style="text-align: right;">100</td>
    </tr>
    <tr style="background: #f9f9f9;"><td style="text-align: center; font-weight: 600;">MiniWoB++</td><td style="text-align: center;">合成</td><td style="text-align: center;">❌</td><td style="text-align: center;">✔️</td><td style="text-align: right;">100</td><td style="text-align: right;">100</td></tr>
    <tr><td style="text-align: center; font-weight: 600;">WebArena</td><td style="text-align: center;">半真实</td><td style="text-align: center;">❌</td><td style="text-align: center;">✔️</td><td style="text-align: right;">4</td><td style="text-align: right;">812</td></tr>
    <tr style="background: #f9f9f9;"><td style="text-align: center; font-weight: 600;">VisualWebArena</td><td style="text-align: center;">半真实</td><td style="text-align: center;">❌</td><td style="text-align: center;">✔️</td><td style="text-align: right;">3</td><td style="text-align: right;">910</td></tr>
    <tr><td style="text-align: center; font-weight: 600;">REAL</td><td style="text-align: center;">半真实</td><td style="text-align: center;">❌</td><td style="text-align: center;">✔️</td><td style="text-align: right;">11</td><td style="text-align: right;">112</td></tr>
    <tr style="background: #f9f9f9;"><td style="text-align: center; font-weight: 600;">WebLINX</td><td style="text-align: center;">真实</td><td style="text-align: center;">❌</td><td style="text-align: center;">❌</td><td style="text-align: right;">155</td><td style="text-align: right;">1,368</td></tr>
    <tr><td style="text-align: center; font-weight: 600;">Mind2Web</td><td style="text-align: center;">真实</td><td style="text-align: center;">❌</td><td style="text-align: center;">❌</td><td style="text-align: right;">137</td><td style="text-align: right;">1,341</td></tr>
    <tr style="background: #f9f9f9;"><td style="text-align: center; font-weight: 600;">Mind2Web-Live</td><td style="text-align: center;">真实</td><td style="text-align: center;">✔️</td><td style="text-align: center;">✔️</td><td style="text-align: right;">46</td><td style="text-align: right;">104</td></tr>
    <tr><td style="text-align: center; font-weight: 600;">MMInA</td><td style="text-align: center;">真实</td><td style="text-align: center;">✔️</td><td style="text-align: center;">✔️</td><td style="text-align: right;">14</td><td style="text-align: right;">1,050</td></tr>
    <tr style="background: #f9f9f9;"><td style="text-align: center; font-weight: 600;">AssistantBench</td><td style="text-align: center;">真实</td><td style="text-align: center;">✔️</td><td style="text-align: center;">✔️</td><td style="text-align: right;">258</td><td style="text-align: right;">214</td></tr>
    <tr><td style="text-align: center; font-weight: 600;">WebVoyager</td><td style="text-align: center;">真实</td><td style="text-align: center;">✔️</td><td style="text-align: center;">✔️</td><td style="text-align: right;">15</td><td style="text-align: right;">643</td></tr>
    <tr style="background: #f9f9f9;"><td style="text-align: center; font-weight: 600;">Bearcubs</td><td style="text-align: center;">真实</td><td style="text-align: center;">✔️</td><td style="text-align: center;">✔️</td><td style="text-align: right;">108</td><td style="text-align: right;">111</td></tr>
    <tr><td style="text-align: center; font-weight: 600;">Online-Mind2Web</td><td style="text-align: center;">真实</td><td style="text-align: center;">✔️</td><td style="text-align: center;">✔️</td><td style="text-align: right;">136</td><td style="text-align: right;">300</td></tr>
    <tr style="background: #f9f9f9;">
      <td rowspan="6" style="vertical-align: middle; text-align: center; background: transparent;"><b>专业</b></td>
      <td style="text-align: center; font-weight: 600;">WebShop</td><td style="text-align: center;">半真实</td><td style="text-align: center;">❌</td><td style="text-align: center;">✔️</td><td style="text-align: right;">1</td><td style="text-align: right;">500</td>
    </tr>
    <tr><td style="text-align: center; font-weight: 600;">ST-WebAgentBench</td><td style="text-align: center;">半真实</td><td style="text-align: center;">❌</td><td style="text-align: center;">✔️</td><td style="text-align: right;">3</td><td style="text-align: right;">222</td></tr>
    <tr style="background: #f9f9f9;"><td style="text-align: center; font-weight: 600;">Wonderbread</td><td style="text-align: center;">半真实</td><td style="text-align: center;">❌</td><td style="text-align: center;">✔️</td><td style="text-align: right;">4</td><td style="text-align: right;">598</td></tr>
    <tr><td style="text-align: center; font-weight: 600;">WorkArena</td><td style="text-align: center;">真实</td><td style="text-align: center;">✔️</td><td style="text-align: center;">✔️</td><td style="text-align: right;">5</td><td style="text-align: right;">33</td></tr>
    <tr style="background: #f9f9f9;"><td style="text-align: center; font-weight: 600;">WorkArena++</td><td style="text-align: center;">真实</td><td style="text-align: center;">✔️</td><td style="text-align: center;">✔️</td><td style="text-align: right;">5</td><td style="text-align: right;">682</td></tr>
    <tr><td style="text-align: center; font-weight: 600;">OmniACT</td><td style="text-align: center;">真实</td><td style="text-align: center;">✔️</td><td style="text-align: center;">✔️</td><td style="text-align: right;">27</td><td style="text-align: right;">736</td></tr>
    <tr style="background: #f9f9f9;">
      <td style="vertical-align: middle; text-align: center; background: transparent;"><b>通用与专业</b></td>
      <td style="text-align: center; font-weight: 600;">WebRetriever</td><td style="text-align: center;"><b>真实</b></td><td style="text-align: center;"><b>✔️</b></td><td style="text-align: center;"><b>✔️</b></td><td style="text-align: right; font-size: 1.1em;"><b>800</b></td><td style="text-align: right; font-size: 1.1em;"><b>1,550</b></td>
    </tr>
  </tbody>
</table>

### 三种评估协议（1,550 个任务）

| 协议 | 说明 |
|----------|-------------|
| **协议 I** | 仅导航：根据任务描述到达目标页面（1,000 个任务） |
| **协议 II** | 在操作文档指导下进行导航（1,000 个任务） |
| **协议 III** | 端到端：导航 + 信息提取（100 个任务） |

值得注意的是，协议 I 和协议 II 有 550 个重叠任务，两者唯一的区别是是否提供操作文档。

## 引用

```bibtex
@misc{dong2026webretrieverlargescalecomprehensivebenchmark,
  title={WebRetriever: A Large-Scale Comprehensive Benchmark for Efficient Web Agent Evaluation},
  author={Wei Dong and Tianyu Fu and Zhe Yu and Hanning Wang and Anyang Su and Zhizhou Fang and Yuyang Chen and Shuo Wang and Minghui Wu and Ping Jiang and Zhen Lei and Chenxu Zhao},
  year={2026},
  eprint={2607.06118},
  archivePrefix={arXiv},
  primaryClass={cs.CV},
  url={https://arxiv.org/abs/2607.06118},
}
```

## 许可证

本数据集采用 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) 许可证发布。
