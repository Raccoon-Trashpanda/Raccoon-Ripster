// ESM wrapper for the shared reaper (Node twin of reaper.py). See reaper.js.
import { createRequire } from 'node:module';
const req = createRequire(import.meta.url);
const r = req('./reaper.js');

export const PREFIX = r.PREFIX;
export const STALE_GRACE_SEC = r.STALE_GRACE_SEC;
export const findChrome = r.findChrome;
export const isOwnedCmdline = r.isOwnedCmdline;
export const extractUserDataDir = r.extractUserDataDir;
export const selectOwned = r.selectOwned;
export const newProfileDir = r.newProfileDir;
export const killTreeSync = r.killTreeSync;
export const listProcs = r.listProcs;
export const sweepStale = r.sweepStale;
export const launchOwnedChrome = r.launchOwnedChrome;
export const withOwnedChrome = r.withOwnedChrome;
export const removeDirTolerant = r.removeDirTolerant;
export const freePort = r.freePort;
export default r;
