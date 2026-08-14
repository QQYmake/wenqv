---
name: wenqu-compare
description: 问渠成长比较 Skill。用于训练末尾比较同一 training 的教案 v1 与 v2，并在同质性门槛通过时与 previous_training_id 指向的上一次训练比较；首次建立行为基线，输出下一次唯一优先目标。
---

# 问渠成长比较

## 前置检查

读取当前训练的 current.md、stage_v1.md、lesson-v1.md、stage_v2.md、lesson-v2.md。lesson-v2.md 必须含 LESSON_V2_DONE，stage_v2.md 必须含 REVIEW_DONE；缺失时路由到 wenqu-iterate。

## 本次训练比较

始终比较本 training 的 v1 与 v2：

- 八维分数与原文证据变化
- 关键修改是否真实落地
- 试讲和反思如何影响修改
- 仍未解决的问题

同一 training 的 v1→v2 是变量更受控的主要证据。

## 跨训练比较

读取 current.md 的 previous_training_id。

- 为 null：只建立本次行为档案基线，不写“成长变化”。
- 有值：读取上一次训练的 current.md、stage_v1.md、stage_v2.md。
- 只有学科、课型、模式三者相同，才允许比较跨 training 八维分数。
- 门槛不通过时，只比较证据质量、跳过行为、课堂理答模式和修改落地，并说明不能比分数的原因。
- 课题也相同时，可注明“同课题复训，比较价值更高”。

只与 previous_training_id 指向的上一次比较，不堆叠全部历史。输出重复问题、新问题、上次目标完成情况、听—判—接—引—验变化，以及下一次唯一优先目标。没有真实试讲记录时如实注明。

雷达图工具未接入时只输出结构化分数，不伪造图表或链接。

## 落盘

将比较结果写入 stage_v2.md 的“成长比较”章节，末尾写入：

    <!-- GROWTH_DONE -->

current.md 中 growth 设为 done，current_skill 设为 completed，current_persona 保持 coach，更新 updated_at。

## 约束

- 不把一次分数上升直接解释为真实教学能力提高。
- 首次训练不输出跨训练成长结论。
- 同质性门槛未通过时不比较绝对分数。
- 写入前确认 owner。
