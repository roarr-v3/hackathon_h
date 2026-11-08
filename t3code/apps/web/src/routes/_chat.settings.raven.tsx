import { createFileRoute } from "@tanstack/react-router";

import { RavenSettings } from "../components/settings/RavenSettings";

export const Route = createFileRoute("/_chat/settings/raven")({
  component: RavenSettings,
});
