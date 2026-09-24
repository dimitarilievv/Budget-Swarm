/**
 * Inline SVG icon set — a small, dependency-free replacement for the emoji the UI used to
 * lean on. Every icon is a 24×24 stroke glyph that inherits `currentColor` and takes a
 * `className` for sizing, so it styles like text. Line-art (lucide-style) keeps the look
 * calm and modern rather than the busy, templated feel of coloured emoji.
 */
import type { SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement>;

function Svg({ children, ...props }: IconProps) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...props}
    >
      {children}
    </svg>
  );
}

export function IconSearch(props: IconProps) {
  return (
    <Svg {...props}>
      <circle cx="11" cy="11" r="7" />
      <path d="m21 21-4.3-4.3" />
    </Svg>
  );
}

export function IconBriefcase(props: IconProps) {
  return (
    <Svg {...props}>
      <rect x="2" y="7" width="20" height="14" rx="2" />
      <path d="M16 7V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v2M2 13h20" />
    </Svg>
  );
}

export function IconCompare(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M3 3v18h18M8 17V9M13 17V5M18 17v-4" />
    </Svg>
  );
}

export function IconUser(props: IconProps) {
  return (
    <Svg {...props}>
      <circle cx="12" cy="8" r="4" />
      <path d="M5.5 20a7 7 0 0 1 13 0" />
    </Svg>
  );
}

export function IconPlane(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M17.8 19.2 16 11l3.5-3.5C21 6 21.5 4 21 3c-1-.5-3 0-4.5 1.5L13 8 4.8 6.2c-.5-.1-.9.1-1.1.5l-.3.5c-.2.5-.1 1 .3 1.3L9 12l-2 3H4l-1 1 3 2 2 3 1-1v-3l3-2 3.5 5.3c.3.4.8.5 1.3.3l.5-.2c.4-.3.6-.7.5-1.2z" />
    </Svg>
  );
}

export function IconCar(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M19 17h2a1 1 0 0 0 1-1v-3c0-.9-.7-1.7-1.5-1.9C18.7 10.6 16 10 16 10s-1.3-1.4-2.2-2.3A2.4 2.4 0 0 0 12 7H5c-.6 0-1.1.4-1.4.9l-1.4 2.9A3.7 3.7 0 0 0 2 12v4a1 1 0 0 0 1 1h2M9 17h6" />
      <circle cx="7" cy="17" r="2" />
      <circle cx="17" cy="17" r="2" />
    </Svg>
  );
}

export function IconBus(props: IconProps) {
  return (
    <Svg {...props}>
      <rect x="3" y="4" width="18" height="13" rx="2" />
      <path d="M3 11h18M7 17v2M17 17v2" />
      <circle cx="7.5" cy="14" r="0.6" fill="currentColor" stroke="none" />
      <circle cx="16.5" cy="14" r="0.6" fill="currentColor" stroke="none" />
    </Svg>
  );
}

export function IconTrain(props: IconProps) {
  return (
    <Svg {...props}>
      <rect x="5" y="3" width="14" height="14" rx="3" />
      <path d="M5 11h14M8 17l-2 3M16 17l2 3" />
      <circle cx="9" cy="14" r="0.6" fill="currentColor" stroke="none" />
      <circle cx="15" cy="14" r="0.6" fill="currentColor" stroke="none" />
    </Svg>
  );
}

export function IconLandmark(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M12 3 4 9h16zM6 9v9M10 9v9M14 9v9M18 9v9M4 21h16" />
    </Svg>
  );
}

export function IconUtensils(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M4 3v6a2 2 0 0 0 4 0V3M6 3v18M16 3c-1.5 0-2.5 2-2.5 5s1 4 2.5 4v9" />
    </Svg>
  );
}

export function IconMountain(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M2 20h20L15 8l-3 5-3-6z" />
    </Svg>
  );
}

export function IconMoon(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9z" />
    </Svg>
  );
}

export function IconCamera(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3z" />
      <circle cx="12" cy="13" r="3" />
    </Svg>
  );
}

export function IconWallet(props: IconProps) {
  return (
    <Svg {...props}>
      <rect x="3" y="6" width="18" height="13" rx="2" />
      <path d="M3 10h18" />
      <circle cx="16.5" cy="14" r="1" fill="currentColor" stroke="none" />
    </Svg>
  );
}

export function IconCalendar(props: IconProps) {
  return (
    <Svg {...props}>
      <rect x="3" y="4" width="18" height="18" rx="2" />
      <path d="M3 10h18M8 2v4M16 2v4" />
    </Svg>
  );
}

export function IconUsers(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" />
      <circle cx="9" cy="7" r="4" />
      <path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75" />
    </Svg>
  );
}

export function IconGauge(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M12 13 15.5 9.5" />
      <path d="M4.6 17.6a8 8 0 1 1 14.8 0z" />
      <circle cx="12" cy="13" r="1" fill="currentColor" stroke="none" />
    </Svg>
  );
}

export function IconArrowRight(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M5 12h14M13 6l6 6-6 6" />
    </Svg>
  );
}

export function IconInfo(props: IconProps) {
  return (
    <Svg {...props}>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 11v5M12 8h.01" />
    </Svg>
  );
}

export function IconMap(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M9 4 3 6v14l6-2 6 2 6-2V4l-6 2-6-2zM9 4v14M15 6v14" />
    </Svg>
  );
}

export function IconLocation(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M12 21s7-5.6 7-11a7 7 0 1 0-14 0c0 5.4 7 11 7 11z" />
      <circle cx="12" cy="10" r="2.5" />
    </Svg>
  );
}

export function IconBed(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M3 7v12M3 13h18v6M21 13v-1a3 3 0 0 0-3-3h-7v4" />
      <circle cx="7" cy="11" r="1.4" fill="currentColor" stroke="none" />
    </Svg>
  );
}

export function IconStar(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M12 3.5l2.6 5.3 5.9.9-4.2 4.1 1 5.8L12 17l-5.3 2.8 1-5.8L3.5 9.7l5.9-.9z" />
    </Svg>
  );
}

export function IconHeart(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M12 20s-7-4.4-9.2-8.7C1.3 8 3 5 6 5c1.8 0 3.1 1 4 2 0.9-1 2.2-2 4-2 3 0 4.7 3 3.2 6.3C19 15.6 12 20 12 20z" />
    </Svg>
  );
}

export function IconCheck(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M20 6 9 17l-5-5" />
    </Svg>
  );
}

export function IconX(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M18 6 6 18M6 6l12 12" />
    </Svg>
  );
}

export function IconSun(props: IconProps) {
  return (
    <Svg {...props}>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
    </Svg>
  );
}

export function IconCloud(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M17.5 19a4.5 4.5 0 0 0 .5-8.97A6 6 0 0 0 6.34 11.1 3.5 3.5 0 0 0 7 19z" />
    </Svg>
  );
}

export function IconCloudSun(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M8 4V2M4.2 5.6 2.8 4.2M4 10H2M6.3 7.8A3.5 3.5 0 0 1 12 8.4" />
      <path d="M17.5 20a4.5 4.5 0 0 0 .3-8.99A5.5 5.5 0 0 0 7.5 12 3.5 3.5 0 0 0 8 20z" />
    </Svg>
  );
}

export function IconCloudRain(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M17.5 15a4.5 4.5 0 0 0 .5-8.97A6 6 0 0 0 6.34 7.1 3.5 3.5 0 0 0 7 14" />
      <path d="M8 17v3M12 18v3M16 17v3" />
    </Svg>
  );
}

/** BudgetSwarm mark — three connected nodes evoking a coordinating swarm of agents. */
export function IconSwarm(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M8.7 9.3 11 14.2M14.7 8.4 13 14.1M9.2 8.1 14 7.2" />
      <circle cx="7" cy="8" r="2.1" fill="currentColor" stroke="none" />
      <circle cx="16" cy="7" r="2.1" fill="currentColor" stroke="none" />
      <circle cx="12" cy="16" r="2.1" fill="currentColor" stroke="none" />
    </Svg>
  );
}
