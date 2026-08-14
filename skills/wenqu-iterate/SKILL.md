---
name: wenqu-iterate
description: 问渠评课反思与教案迭代 Skill。用于基于教案 v1、八维诊断和真实试讲记录进行证据化评课，逐轮提出一个反思问题，生成有修改前后对照的教案 v2，并完成同锚点八维复诊。
---

# 问渠评课与教案迭代

## 前置检查

读取当前训练的 current.md、lesson-v1.md、stage_v1.md；lesson-v1.md 必须含 LESSON_V1_DONE。若 rehearsal 为 done，再读 stage_v2.md 的真实试讲记录；若为 skipped，明确评课证据仅限教案和诊断。

## 工作流

1. 生成评课报告：逐项引用教案或试讲原文证据，判断优势、问题和影响。
2. deep 模式进行递进反思：一次只问一个来自真实设计或回应的问题，等用户回答后再问下一个。
3. fast 模式只给 3—5 个核心问题，直接进入迭代。
4. 根据诊断、试讲和用户反思生成 lesson-v2.md。每项关键修改说明：
   - 修改前
   - 修改后
   - 修改原因与证据
   - 预计学生变化
   - 对应改善维度
5. 对 v2 使用与 v1 完全相同的八维和评分锚点复诊。每维记录分数、变化方向和证据。

禁止只做语言润色。跳过试讲时不得引用虚构的课堂表现。

## 落盘

- 写入 lesson-v2.md，并以以下标记结束：

      <!-- LESSON_V2_DONE -->

- 把评课、反思和 v2 八维复诊写入 stage_v2.md，并以以下标记结束：

      <!-- REVIEW_DONE -->

- current.md 中 review 设为 done，current_skill 设为 wenqu-compare，更新 updated_at。

## 约束

- 反思一次只问一个问题。
- 每个判断和关键修改都要有真实证据。
- fast 模式也必须完整保存 v2 八维复诊。
- 写入前重读文件并确认 owner。
