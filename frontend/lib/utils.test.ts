import { describe, it, expect } from "vitest";
import { cn } from "./utils";

describe("cn helper utility", () => {
  it("should merge class names correctly", () => {
    expect(cn("bg-red-500", "text-white")).toBe("bg-red-500 text-white");
  });

  it("should resolve Tailwind conflicts correctly", () => {
    expect(cn("p-4", "p-2")).toBe("p-2");
  });

  it("should handle conditional class names", () => {
    expect(cn("p-4", false && "bg-blue-500", "m-2")).toBe("p-4 m-2");
  });
});
