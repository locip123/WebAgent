"""Version-locked Rebrowser forward port for the bundled Playwright driver.

``rebrowser-patches`` currently publishes a Python Playwright distribution for
1.52 only.  WebRetriever requires 1.61, whose Node driver bundles the relevant
modules into ``coreBundle.js``.  This module applies the equivalent Chromium
CDP changes only while an explicitly selected Rebrowser connection is alive.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from threading import RLock
from typing import Callable

__all__ = [
	'RebrowserDriverPatch',
	'RebrowserPatchError',
	'RebrowserPatchInfo',
	'activate_rebrowser_driver',
	'rebrowser_runtime_fix_mode',
]


class RebrowserPatchError(RuntimeError):
	"""The installed Playwright driver cannot safely receive the forward port."""


@dataclass(frozen=True, slots=True)
class RebrowserPatchInfo:
	"""The exact driver revision guarded by this forward port."""

	driver_version: str
	base_sha256: str
	patched_sha256: str


class RebrowserDriverPatch:
	"""Atomically patch and restore the known Playwright 1.61 Chromium driver.

	The lock and lease counter deliberately cover the lifetime of the client
	manager, rather than just ``connect_over_cdp``.  The Python SDK launches its
	Node driver after ``async_playwright().__aenter__()``, so restoring earlier
	would make the reported ``rebrowser`` run indistinguishable from Playwright.
	"""

	_EXPECTED_DRIVER_VERSION = '1.61.1-beta-1782139630000'
	_EXPECTED_BASE_SHA256 = '6be5c2ea035554e9b184b1dbc7aa5e7f1fb428dd1b5c202022858dcfae9bee27'
	_MARKER = '/* webretriever-rebrowser-forward-port: playwright-1.61 */\n'

	def __init__(
		self,
		*,
		package_dir: Path | None = None,
		expected_driver_version: str | None = None,
		expected_base_sha256: str | None = None,
	) -> None:
		self._package_dir = package_dir
		self._expected_driver_version = expected_driver_version or self._EXPECTED_DRIVER_VERSION
		self._expected_base_sha256 = expected_base_sha256 or self._EXPECTED_BASE_SHA256
		self._lock = RLock()
		self._leases = 0
		self._active_path: Path | None = None
		self._original: bytes | None = None
		self._patched: bytes | None = None

	def acquire(self) -> Callable[[], None]:
		"""Apply the verified forward port and return an idempotent release hook."""

		with self._lock:
			bundle_path = self._bundle_path()
			if self._leases:
				if bundle_path != self._active_path:
					raise RebrowserPatchError('the active Rebrowser patch belongs to a different Playwright driver')
				if bundle_path.read_bytes() != self._patched:
					raise RebrowserPatchError('the active Rebrowser driver changed while a connection was open')
				self._leases += 1
			else:
				self._assert_expected_driver(bundle_path)
				original = bundle_path.read_bytes()
				patched = self._build_patch(original)
				self._atomic_write(bundle_path, patched)
				self._active_path = bundle_path
				self._original = original
				self._patched = patched
				self._leases = 1

		released = False

		def release() -> None:
			nonlocal released
			if released:
				return
			released = True
			self.release()

		return release

	def release(self) -> None:
		"""Release one connection lease and restore the unmodified Node driver."""

		with self._lock:
			if self._leases < 1 or self._active_path is None or self._original is None or self._patched is None:
				raise RebrowserPatchError('Rebrowser driver release without an active patch')
			self._leases -= 1
			if self._leases:
				return
			try:
				if self._active_path.read_bytes() != self._patched:
					raise RebrowserPatchError('refusing to restore a Rebrowser driver changed by another process')
				self._atomic_write(self._active_path, self._original)
			finally:
				self._active_path = None
				self._original = None
				self._patched = None

	def active_info(self) -> RebrowserPatchInfo | None:
		"""Return the current verified revision without exposing driver contents."""

		with self._lock:
			if self._patched is None:
				return None
			return RebrowserPatchInfo(
				driver_version=self._expected_driver_version,
				base_sha256=self._expected_base_sha256,
				patched_sha256=self._sha256(self._patched),
			)

	def restore_if_active(self) -> None:
		"""Best-effort restoration used only during interpreter shutdown."""

		with self._lock:
			if self._active_path is None or self._original is None or self._patched is None:
				return
			if self._active_path.read_bytes() == self._patched:
				self._atomic_write(self._active_path, self._original)
			self._leases = 0
			self._active_path = None
			self._original = None
			self._patched = None

	def _bundle_path(self) -> Path:
		package_dir = self._package_dir or self._installed_package_dir()
		bundle_path = package_dir / 'lib' / 'coreBundle.js'
		if not bundle_path.is_file():
			raise RebrowserPatchError(f'Playwright driver bundle was not found at {bundle_path}')
		return bundle_path

	@staticmethod
	def _installed_package_dir() -> Path:
		try:
			playwright = import_module('playwright')
		except ImportError as exc:
			raise RebrowserPatchError('Playwright must be installed before Rebrowser can be selected') from exc
		module_path = getattr(playwright, '__file__', None)
		if not module_path:
			raise RebrowserPatchError('could not locate the installed Playwright Python package')
		return Path(module_path).resolve().parent / 'driver' / 'package'

	def _assert_expected_driver(self, bundle_path: Path) -> None:
		package_json = bundle_path.parent.parent / 'package.json'
		try:
			payload = json.loads(package_json.read_text(encoding='utf-8'))
		except (OSError, json.JSONDecodeError) as exc:
			raise RebrowserPatchError(f'could not read Playwright driver manifest: {package_json}') from exc
		driver_version = payload.get('version')
		if driver_version != self._expected_driver_version:
			raise RebrowserPatchError(
				'Rebrowser forward port supports only Playwright driver '
				f'{self._expected_driver_version}, found {driver_version!r}'
			)
		actual_sha256 = self._sha256(bundle_path.read_bytes())
		if actual_sha256 != self._expected_base_sha256:
			raise RebrowserPatchError(
				'Rebrowser forward port refused an unrecognised Playwright driver hash: '
				f'expected {self._expected_base_sha256}, found {actual_sha256}'
			)

	def _build_patch(self, original: bytes) -> bytes:
		if self._sha256(original) != self._expected_base_sha256:
			raise RebrowserPatchError('the Playwright driver changed before the Rebrowser patch could be applied')
		try:
			source = original.decode('utf-8')
		except UnicodeDecodeError as exc:
			raise RebrowserPatchError('the Playwright driver bundle is not UTF-8 text') from exc
		if self._MARKER in source:
			raise RebrowserPatchError('the Playwright driver is already marked as Rebrowser-patched')

		for before, after, description in _FORWARD_PORT_REPLACEMENTS:
			source = self._replace_once(source, before, after, description)
		return (self._MARKER + source).encode('utf-8')

	@staticmethod
	def _replace_once(source: str, before: str, after: str, description: str) -> str:
		count = source.count(before)
		if count != 1:
			raise RebrowserPatchError(f'expected one {description} anchor in Playwright driver, found {count}')
		return source.replace(before, after, 1)

	@staticmethod
	def _sha256(payload: bytes) -> str:
		return hashlib.sha256(payload).hexdigest()

	@staticmethod
	def _atomic_write(path: Path, payload: bytes) -> None:
		mode = path.stat().st_mode
		file_descriptor, temporary_name = tempfile.mkstemp(prefix=f'.{path.name}.rebrowser-', dir=path.parent)
		try:
			with os.fdopen(file_descriptor, 'wb') as temporary_file:
				temporary_file.write(payload)
				temporary_file.flush()
				os.fsync(temporary_file.fileno())
			os.chmod(temporary_name, mode)
			os.replace(temporary_name, path)
		except BaseException:
			try:
				Path(temporary_name).unlink(missing_ok=True)
			except OSError:
				pass
			raise


_DEFAULT_PATCH = RebrowserDriverPatch()
_SUPPORTED_RUNTIME_FIX_MODES = frozenset({'addBinding', 'alwaysIsolated', 'enableDisable', '0'})


def _restore_default_patch_at_exit() -> None:
	"""Best-effort cleanup for an interrupted local development process."""

	try:
		_DEFAULT_PATCH.restore_if_active()
	except RebrowserPatchError:
		pass


atexit.register(_restore_default_patch_at_exit)


def activate_rebrowser_driver() -> Callable[[], None]:
	"""Acquire the process-wide verified Rebrowser driver patch."""

	rebrowser_runtime_fix_mode()
	return _DEFAULT_PATCH.acquire()


def rebrowser_runtime_fix_mode() -> str:
	"""Read the supported mode that the spawned Node driver will observe."""

	mode = os.getenv('REBROWSER_PATCHES_RUNTIME_FIX_MODE', 'addBinding').strip() or 'addBinding'
	if mode not in _SUPPORTED_RUNTIME_FIX_MODES:
		raise RebrowserPatchError(
			'unsupported REBROWSER_PATCHES_RUNTIME_FIX_MODE '
			f'{mode!r}; supported values are {sorted(_SUPPORTED_RUNTIME_FIX_MODES)}'
		)
	return mode


_FORWARD_PORT_REPLACEMENTS: tuple[tuple[str, str, str], ...] = (
	(
		"""        this.utilityWorldName = `__playwright_utility_world_${this._page.guid}`;""",
		"""        this.utilityWorldName = process.env["REBROWSER_PATCHES_UTILITY_WORLD_NAME"] !== "0" ? process.env["REBROWSER_PATCHES_UTILITY_WORLD_NAME"] || "util" : `__playwright_utility_world_${this._page.guid}`;""",
		'utility-world-name',
	),
	(
		"""      dispose() {
        this._closed = true;
        this._connection._sessions.delete(this._sessionId);
        for (const callback of this._callbacks.values()) {
          callback.error.setMessage(`Internal server error, session closed.`);
          callback.error.type = this._crashed ? "crashed" : "closed";
          callback.error.logs = this._connection._browserDisconnectedLogs;
          callback.reject(callback.error);
        }
        this._callbacks.clear();
      }
    };
    CDPSession = class _CDPSession""",
		"""      dispose() {
        this._closed = true;
        this._connection._sessions.delete(this._sessionId);
        for (const callback of this._callbacks.values()) {
          callback.error.setMessage(`Internal server error, session closed.`);
          callback.error.type = this._crashed ? "crashed" : "closed";
          callback.error.logs = this._connection._browserDisconnectedLogs;
          callback.reject(callback.error);
        }
        this._callbacks.clear();
      }
      async __webretrieverRebrowserEmitExecutionContext({ world, targetId, utilityWorldName, isWorker = false }) {
        const fixMode = process.env["REBROWSER_PATCHES_RUNTIME_FIX_MODE"] || "addBinding";
        if (fixMode === "0")
          return;
        let contextPayload;
        if (fixMode === "alwaysIsolated" || world === "utility") {
          const { executionContextId } = await this.send("Page.createIsolatedWorld", {
            frameId: targetId,
            worldName: utilityWorldName,
            grantUniveralAccess: true
          });
          contextPayload = {
            id: executionContextId,
            name: world === "utility" ? utilityWorldName : "",
            auxData: { frameId: targetId, isDefault: world !== "utility" }
          };
        } else if (fixMode === "enableDisable") {
          await this.send("Runtime.enable");
          await this.send("Runtime.disable");
          return;
        } else {
          const bindingName = Array.from({ length: 16 }, () => Math.random().toString(36).slice(2)).join("");
          let contextId;
          const bindingCalledHandler = ({ name, payload, executionContextId }) => {
            if (name !== bindingName || payload !== targetId || contextId !== void 0)
              return;
            contextId = executionContextId;
          };
          this.on("Runtime.bindingCalled", bindingCalledHandler);
          try {
            await this.send("Runtime.addBinding", { name: bindingName });
            if (isWorker) {
              await this.send("Runtime.evaluate", { expression: `this['${bindingName}']('${targetId}')` });
            } else {
              await this.send("Page.addScriptToEvaluateOnNewDocument", {
                source: `document.addEventListener('${bindingName}', event => self['${bindingName}'](event.detail.frameId))`,
                runImmediately: true
              });
              const { executionContextId } = await this.send("Page.createIsolatedWorld", {
                frameId: targetId,
                worldName: bindingName,
                grantUniveralAccess: true
              });
              await this.send("Runtime.evaluate", {
                expression: `document.dispatchEvent(new CustomEvent('${bindingName}', { detail: { frameId: '${targetId}' } }))`,
                contextId: executionContextId
              });
            }
            if (contextId === void 0)
              throw new Error("Rebrowser binding did not yield an execution context");
          } finally {
            this.off("Runtime.bindingCalled", bindingCalledHandler);
          }
          contextPayload = { id: contextId, name: "", auxData: { frameId: targetId, isDefault: true } };
        }
        this.emit("Runtime.executionContextCreated", { context: contextPayload });
      }
    };
    CDPSession = class _CDPSession""",
		'CRSession disposal block',
	),
	(
		"""          this._client.send("Runtime.enable", {}),
          this._client.send("Page.addScriptToEvaluateOnNewDocument", {""",
		"""          process.env["REBROWSER_PATCHES_RUNTIME_FIX_MODE"] === "0" ? this._client.send("Runtime.enable", {}) : Promise.resolve(),
          this._client.send("Page.addScriptToEvaluateOnNewDocument", {""",
		'main-frame Runtime.enable call',
	),
	(
		"""        this.onLifecycleEvent("commit");
      }
      _setPendingDocument""",
		"""        this.onLifecycleEvent("commit");
        if (process.env["REBROWSER_PATCHES_RUNTIME_FIX_MODE"] !== "0") {
          const crPage = this._page.delegate;
          const session2 = crPage._sessions.get(this._id) || crPage._mainFrameSession;
          session2?._client.emit("Runtime.executionContextsCleared");
        }
      }
      _setPendingDocument""",
		'frame lifecycle reset',
	),
	(
		"""      context(world) {
        if (this._page.delegate.noUtilityWorld?.())
          world = "main";
        return this._contextData.get(world).contextPromise.then((contextOrDestroyedReason) => {
          if (contextOrDestroyedReason instanceof ExecutionContext)
            return contextOrDestroyedReason;
          throw new Error(contextOrDestroyedReason.destroyedReason);
        });
      }
      mainContext() {""",
		"""      context(world, useContextPromise = false) {
        if (this._page.delegate.noUtilityWorld?.())
          world = "main";
        const contextData = this._contextData.get(world);
        if (process.env["REBROWSER_PATCHES_RUNTIME_FIX_MODE"] === "0" || contextData.context || useContextPromise) {
          return contextData.contextPromise.then((contextOrDestroyedReason) => {
            if (contextOrDestroyedReason instanceof ExecutionContext)
              return contextOrDestroyedReason;
            throw new Error(contextOrDestroyedReason.destroyedReason);
          });
        }
        const crPage = this._page.delegate;
        const session2 = crPage._sessions.get(this._id) || crPage._mainFrameSession;
        return session2._client.__webretrieverRebrowserEmitExecutionContext({
          world,
          targetId: this._id,
          utilityWorldName: crPage.utilityWorldName
        }).then(() => this.context(world, true));
      }
      mainContext() {""",
		'frame context resolver',
	),
	(
		"""      static async dispatch(page, payload, context2) {
        const { name, seq, serializedArgs } = JSON.parse(payload);""",
		"""      static async dispatch(page, payload, context2) {
        if (process.env["REBROWSER_PATCHES_RUNTIME_FIX_MODE"] !== "0" && (typeof payload !== "string" || !payload.startsWith("{")))
          return;
        const { name, seq, serializedArgs } = JSON.parse(payload);""",
		'page-binding dispatch',
	),
	(
		"""      constructor(parent, url2, onDisconnect) {
        super(parent, "worker");
        this._executionContextPromise = new ManualPromise();
        this._workerScriptLoaded = false;
        this.existingExecutionContext = null;
        this.openScope = new LongStandingScope();
        this.attribution.worker = this;
        this.url = url2;
        this._onDisconnect = onDisconnect;
      }""",
		"""      constructor(parent, url2, onDisconnect, rebrowserSession, rebrowserTargetId) {
        super(parent, "worker");
        this._executionContextPromise = new ManualPromise();
        this._workerScriptLoaded = false;
        this.existingExecutionContext = null;
        this.openScope = new LongStandingScope();
        this.attribution.worker = this;
        this.url = url2;
        this._onDisconnect = onDisconnect;
        this._rebrowserSession = rebrowserSession;
        this._rebrowserTargetId = rebrowserTargetId;
      }""",
		'worker constructor',
	),
	(
		"""      async evaluateExpression(progress2, expression2, isFunction2, arg) {
        return progress2.race(evaluateExpression(await this._executionContextPromise, expression2, { returnByValue: true, isFunction: isFunction2 }, arg));
      }
      async evaluateExpressionHandle(progress2, expression2, isFunction2, arg) {
        return progress2.race(evaluateExpression(await this._executionContextPromise, expression2, { returnByValue: false, isFunction: isFunction2 }, arg));
      }""",
		"""      async __webretrieverRebrowserContext() {
        if (process.env["REBROWSER_PATCHES_RUNTIME_FIX_MODE"] !== "0" && !this.existingExecutionContext && this._rebrowserSession) {
          await this._rebrowserSession.__webretrieverRebrowserEmitExecutionContext({
            world: "main",
            targetId: this._rebrowserTargetId,
            utilityWorldName: "util",
            isWorker: true
          });
          this.workerScriptLoaded();
        }
        return this._executionContextPromise;
      }
      async evaluateExpression(progress2, expression2, isFunction2, arg) {
        return progress2.race(evaluateExpression(await this.__webretrieverRebrowserContext(), expression2, { returnByValue: true, isFunction: isFunction2 }, arg));
      }
      async evaluateExpressionHandle(progress2, expression2, isFunction2, arg) {
        return progress2.race(evaluateExpression(await this.__webretrieverRebrowserContext(), expression2, { returnByValue: false, isFunction: isFunction2 }, arg));
      }""",
		'worker evaluation methods',
	),
	(
		"""        const worker = new Worker(this._page, url2);
        this._page.addWorker(event.sessionId, worker);""",
		"""        const worker = new Worker(this._page, url2, void 0, session2, event.targetInfo.targetId);
        this._page.addWorker(event.sessionId, worker);""",
		'worker construction',
	),
	(
		"""        session2._sendMayFail("Runtime.enable");
        this._crPage._networkManager.addSession(session2,""",
		"""        if (process.env["REBROWSER_PATCHES_RUNTIME_FIX_MODE"] === "0")
          session2._sendMayFail("Runtime.enable");
        this._crPage._networkManager.addSession(session2,""",
		'worker Runtime.enable call',
	),
	(
		"""        session2.send("Runtime.enable", {}).catch((e) => {
        });
        session2.send("Runtime.runIfWaitingForDebugger").catch((e) => {""",
		"""        if (process.env["REBROWSER_PATCHES_RUNTIME_FIX_MODE"] === "0") {
          session2.send("Runtime.enable", {}).catch((e) => {
          });
        }
        session2.send("Runtime.runIfWaitingForDebugger").catch((e) => {""",
		'service-worker Runtime.enable call',
	),
	(
		"""        Promise.all([
          session2.send("Runtime.enable"),
          session2.send("Runtime.addBinding", { name: kBindingName2 }),""",
		"""        Promise.all([
          process.env["REBROWSER_PATCHES_RUNTIME_FIX_MODE"] === "0" ? session2.send("Runtime.enable") : Promise.resolve(),
          session2.send("Runtime.addBinding", { name: kBindingName2 }),""",
		'DevTools Runtime.enable call',
	),
)
