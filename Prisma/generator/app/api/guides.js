import { apiFetch } from "./client.js";

export function fetchGuideState() {
  return apiFetch("/guides/state");
}

export function putGuideState(state, expectedRevision) {
  return apiFetch("/guides/state", {
    method: "PUT",
    body: JSON.stringify({
      expected_revision: expectedRevision,
      state,
    }),
  });
}

export function prepareBasicsGuide({
  restoreTutorialPrinter = false,
  includeTutorialPrinter = true,
  includeTutorialImage = true,
} = {}) {
  return apiFetch("/guides/basics/prepare", {
    method: "POST",
    body: JSON.stringify({
      restore_tutorial_printer: !!restoreTutorialPrinter,
      include_tutorial_printer: !!includeTutorialPrinter,
      include_tutorial_image: !!includeTutorialImage,
    }),
  });
}
