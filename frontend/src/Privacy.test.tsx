import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { Privacy } from "./Privacy";

describe("Privacy", () => {
  it("renders the privacy heading and analyzer endpoint", () => {
    render(<Privacy />);
    expect(
      screen.getByRole("heading", { name: /privacy/i, level: 1 }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/api-medic\.markandrewmarquez\.com\/api\/analyze/),
    ).toBeInTheDocument();
  });

  it("explains what the extension masks before sending", () => {
    render(<Privacy />);
    expect(
      screen.getByRole("heading", { name: /what is masked before sending/i }),
    ).toBeInTheDocument();
  });

  it("links back to the app root", () => {
    render(<Privacy />);
    const back = screen.getByRole("link", { name: /back to api-medic/i });
    expect(back).toHaveAttribute("href", "/");
  });
});
