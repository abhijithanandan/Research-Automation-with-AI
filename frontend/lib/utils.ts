import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/**
 * Signature emerald focus ring — crisp, keyboard-only (focus-visible so it
 * never fires on mouse click). Compose into any interactive element:
 *   className={cn("...", focusRing)}
 * Per .agents/skills/tailwind-design-system focusRing pattern + the Level-3
 * polish mandate (focus:ring-2 focus:ring-green-500).
 */
export const focusRing =
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2 focus-visible:ring-offset-background";
