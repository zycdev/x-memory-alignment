<!-- camd:checklist {"schema":"camd/v1","source_agent":"codex","target_agent":"claude-code"} -->
# 记忆蒸馏确认清单

<!-- camd:candidate {"schema":"camd/v1","id":"CAMD-ZH-001","status":"conflict","source_agent":"codex","target_agent":"claude-code","origin_agent":"codex","source_scope":"project","target_scope":"project","normalization":"camd-norm/v1","fingerprint":"sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","project":{"source_root":"/workspace/source","target_root":"/workspace/target","git_identity":"example/project","mapping":"confirmed-remote","key":"61306192c2ba41f7"}} -->
- [x] 按以下合并建议处理

对 Claude Code 的用途：为同一项目保留更具体的验证边界。

目标端现有记忆：已有较宽泛规则，需要限定适用范围。

推荐处理：保留两者并明确项目范围。

<!-- camd:proposed:start -->
在 example/project 中，只有验证通过的结果才能写入耐久结论文档。
<!-- camd:proposed:end -->
<!-- /camd:candidate -->
