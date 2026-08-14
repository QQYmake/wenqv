---
name: wenqu-rehearsal
description: 问渠试讲与课堂理答训练 Skill。用于教案 v1 和八维诊断完成后的三轮文字试讲、一轮快速理答或明确跳过；试讲时加载 wenqu-student，记录真实往返，并按听—判—接—引—验评价教师回应。
---

# 问渠试讲训练

## 前置检查

读取当前训练的 current.md、lesson-v1.md 和 stage_v1.md。必须存在 LESSON_V1_DONE 与 DIAGNOSIS_DONE，mode 必须为 deep。缺失时路由到 wenqu-draft；fast 模式已跳过本环节。

## 选择模式

让用户选择：

- 完整三轮文字试讲
- 一轮快速课堂理答
- 跳过训练，直接优化教案

用户跳过时，不生成试讲章节；只将 current.md 的 rehearsal 设为 skipped，current_skill 设为 wenqu-iterate，并明确后续评课证据仅来自教案和诊断。

## 试讲

1. 选择一个核心问题和一个主要错误类型。
2. 用 load_skill 加载 wenqu-student。
3. 将 current_persona 设为 student，并明确提示当前进入学生角色。
4. 学生先回答；用户作为教师回应；学生根据支架更新理解。每轮最多三次往返。
5. 结束本轮后把 current_persona 切回 coach，再进行评价。

按“听—判—接—引—验”评价：

- 听：是否准确回应学生实际表达。
- 判：是否判断学生会什么、卡在哪里。
- 接：是否保留回答中的有效部分。
- 引：是否给出大小合适的下一步支架。
- 验：是否再次检验理解。

说明教师接住了什么、错误原因判断是否合理、是否直接说出答案、支架是否合适、学生是否真正完成自我修正。不要在同一条回复里同时扮演教师、学生和评委。

## 落盘

把真实试讲往返与每轮评价写入 stage_v2.md 的“试讲记录”，末尾写入：

    <!-- REHEARSAL_DONE -->

将 current.md 的 rehearsal 设为 done、current_persona 设为 coach、current_skill 设为 wenqu-iterate，并更新 updated_at。

## 约束

- 不替用户作教师回答。
- 不虚构跳过的试讲记录。
- 每轮最多三次往返。
- 写入前确认当前 conversation 仍拥有训练。
