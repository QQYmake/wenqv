import { Icon } from "./Icon";

const suggestions = [
  {
    label: "任务识别",
    description: "明确学科、课题与学情。",
    prompt: "开始问渠备课训练，先确认学科、课题、课型和学生基础。",
  },
  {
    label: "逐级共创",
    description: "共创目标、重难点、过程与作业。",
    prompt: "深度共创一个高中数学或英语课题，完成目标、重难点、过程和作业。",
  },
  {
    label: "试讲迭代",
    description: "模拟课堂，评课并更新教案。",
    prompt: "继续试讲、评课或迭代教案，先检查当前训练进度。",
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
        <p className="welcome-kicker">WENQU · 高中备课实训</p>
        <h1 id="welcome-title">从课题开始，<br />完成一轮备课训练</h1>
        <p className="welcome-subtitle">面向高中数学、英语师范生与青年教师，完成共创、试讲、评课和迭代。</p>
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
