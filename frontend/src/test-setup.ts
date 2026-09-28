import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// Testing Library only unmounts between tests on its own when vitest globals
// are enabled; they are not, so do it here.
afterEach(cleanup);
