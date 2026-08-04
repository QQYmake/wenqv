import { Icon } from "./Icon";

const suggestions = [
  {
    label: "梳理思路",
    description: "把零散想法整理成清楚的行动路径",
    prompt: "帮我把一个还很模糊的想法梳理成目标、约束与下一步。",
  },
  {
    label: "阅读项目",
    description: "分析材料，保留结构并指出关键问题",
    prompt: "请阅读我接下来提供的项目材料，先还原结构，再指出风险和优先级。",
  },
  {
    label: "一起实现",
    description: "拆解任务，并在工具协作中逐步完成",
    prompt: "和我一起实现一个功能：先确认验收标准，再给出最小可行方案。",
  },
];

export function Welcome({ onSuggestion }: { onSuggestion: (prompt: string) => void }) {
  return (
    <section className="welcome" aria-labelledby="welcome-title">
      <div className="welcome-copy">
        <div className="welcome-orbit" aria-hidden="true">
          <span className="welcome-sun" />
          <span className="welcome-ripple welcome-ripple--one" />
          <span className="welcome-ripple welcome-ripple--two" />
        </div>
        <p className="welcome-kicker">A QUIET PLACE TO THINK</p>
        <h1 id="welcome-title">今天，我们从哪里开始？</h1>
        <p className="welcome-subtitle">把问题放在这里。工具、Skills 与上下文会在需要时自然加入。</p>
      </div>

      <div className="suggestion-grid" aria-label="建议开场">
        {suggestions.map((suggestion, index) => (
          <button
            type="button"
            className="suggestion-card"
            style={{ "--stagger": `${index * 70}ms` } as React.CSSProperties}
            key={suggestion.label}
            onClick={() => onSuggestion(suggestion.prompt)}
          >
            <span className="suggestion-card__inner">
              <span className="suggestion-icon"><Icon name={index === 1 ? "leaf" : "spark"} /></span>
              <span className="suggestion-copy">
                <span className="suggestion-index" aria-hidden="true">0{index + 1}</span>
                <strong>{suggestion.label}</strong>
                <small>{suggestion.description}</small>
              </span>
              <span className="suggestion-arrow-wrap" aria-hidden="true">
                <Icon className="suggestion-arrow" name="chevron" />
              </span>
            </span>
          </button>
        ))}
      </div>
    </section>
  );
}
