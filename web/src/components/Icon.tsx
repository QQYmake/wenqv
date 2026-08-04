import type { SVGProps } from "react";

export type IconName =
  | "add"
  | "arrow-down"
  | "arrow-up"
  | "check"
  | "chevron"
  | "close"
  | "history"
  | "leaf"
  | "menu"
  | "moon"
  | "more"
  | "rename"
  | "send"
  | "settings"
  | "spark"
  | "stop"
  | "sun"
  | "trash";

export function Icon({ name, ...props }: { name: IconName } & SVGProps<SVGSVGElement>) {
  const common = {
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.45,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
  };

  const paths: Record<IconName, React.ReactNode> = {
    add: (
      <>
        <path d="M12 5v14M5 12h14" />
      </>
    ),
    "arrow-down": <path d="M12 5v14m0 0 5-5m-5 5-5-5" />,
    "arrow-up": <path d="M12 19V5m0 0-5 5m5-5 5 5" />,
    check: <path d="m5 12 4 4L19 6" />,
    chevron: <path d="m9 18 6-6-6-6" />,
    close: (
      <>
        <path d="m6 6 12 12" />
        <path d="M18 6 6 18" />
      </>
    ),
    history: (
      <>
        <path d="M3 12a9 9 0 1 0 3-6.7L3 8" />
        <path d="M3 3v5h5M12 7v5l3 2" />
      </>
    ),
    leaf: (
      <>
        <path d="M20 4C12 4 6 7.5 6 13c0 3 2.1 5 5 5 5.5 0 9-6 9-14Z" />
        <path d="M4 20c2.5-5 6.5-8.5 12-11" />
      </>
    ),
    menu: (
      <>
        <path d="M5 7h14M5 12h14M5 17h14" />
      </>
    ),
    moon: <path d="M20 15.4A8 8 0 0 1 8.6 4 8.5 8.5 0 1 0 20 15.4Z" />,
    more: (
      <>
        <circle cx="5" cy="12" r="1" fill="currentColor" stroke="none" />
        <circle cx="12" cy="12" r="1" fill="currentColor" stroke="none" />
        <circle cx="19" cy="12" r="1" fill="currentColor" stroke="none" />
      </>
    ),
    rename: (
      <>
        <path d="M12 20h9" />
        <path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4Z" />
      </>
    ),
    send: <path d="m4 4 17 8-17 8 3-8Zm3 8h14" />,
    settings: (
      <>
        <circle cx="12" cy="12" r="3" />
        <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1a1.7 1.7 0 0 0 1.9.3A1.7 1.7 0 0 0 10 3V2.8h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1Z" />
      </>
    ),
    spark: (
      <>
        <path d="m12 3 1.2 4.2L17 9l-3.8 1.8L12 15l-1.2-4.2L7 9l3.8-1.8Z" />
        <path d="m18.5 15 .6 2.1 1.9.9-1.9.9-.6 2.1-.6-2.1L16 18l1.9-.9Z" />
      </>
    ),
    stop: <rect x="7" y="7" width="10" height="10" rx="2" fill="currentColor" stroke="none" />,
    sun: (
      <>
        <circle cx="12" cy="12" r="4" />
        <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M2 12h2m16 0h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
      </>
    ),
    trash: (
      <>
        <path d="M4 7h16M9 7V4h6v3M7 7l1 13h8l1-13M10 11v5M14 11v5" />
      </>
    ),
  };

  return (
    <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" {...common} {...props}>
      {paths[name]}
    </svg>
  );
}
