import type * as Path from "effect/Path";

type PathOperations = Pick<Path.Path, "join">;

export const ravenSettingsDir = (path: PathOperations, userDataPath: string): string =>
  path.join(userDataPath, "raven");
