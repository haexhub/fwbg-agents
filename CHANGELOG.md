# Changelog

## [0.21.1](https://github.com/haexhub/fwbg-agents/compare/v0.21.0...v0.21.1) (2026-07-12)


### Bug Fixes

* **persistence:** re-attach UTC tzinfo on datetime read ([#165](https://github.com/haexhub/fwbg-agents/issues/165)) ([50e511a](https://github.com/haexhub/fwbg-agents/commit/50e511a3c06d78ef2e3d0096646382f78bbb4c31))

## [0.21.0](https://github.com/haexhub/fwbg-agents/compare/v0.20.0...v0.21.0) (2026-07-12)


### Features

* **persistence:** store runner auto-mode state in state.db ([beb9c42](https://github.com/haexhub/fwbg-agents/commit/beb9c42ebca187c703ea2b2ddda5ac31fcc100c1))
* **persistence:** store runner auto-mode state in state.db instead of JSON file ([e15450d](https://github.com/haexhub/fwbg-agents/commit/e15450d3ea3aab61a635df4280d2a93ac4ae275d))

## [0.20.0](https://github.com/haexhub/fwbg-agents/compare/v0.19.0...v0.20.0) (2026-07-12)


### Features

* **auto-runner:** override abandon when core metrics still improving ([1b783e0](https://github.com/haexhub/fwbg-agents/commit/1b783e09b12a1ac2b831b0137947a7482b6f3c8e))
* **auto-runner:** override abandon when core metrics still improving ([0e1cf64](https://github.com/haexhub/fwbg-agents/commit/0e1cf647ca819669bbec10a13692f7a8f4dc38fc))


### Bug Fixes

* **auto-runner:** retry and abandon stuck PROPOSED strategies ([e7189aa](https://github.com/haexhub/fwbg-agents/commit/e7189aa2f6309abe9769808cb96decf5471d1a2c))
* **auto-runner:** retry and abandon stuck PROPOSED strategies ([f88c5a3](https://github.com/haexhub/fwbg-agents/commit/f88c5a363933985f103483fed151c19728cda432))
* **test:** break long line in abandon-override test (ruff E501) ([7a7faa6](https://github.com/haexhub/fwbg-agents/commit/7a7faa659a7a87586b3d1024829eb63b9955b388))

## [0.19.0](https://github.com/haexhub/fwbg-agents/compare/v0.18.0...v0.19.0) (2026-07-11)


### Features

* **agents:** agent-run timeline + LLM-session instrumentation (Plan 006 backend, steps 1-6) ([8428abd](https://github.com/haexhub/fwbg-agents/commit/8428abd098940f107fb7d298173ceea5dbde9331))
* **agents:** parent_run_id flow drill-down — backend (Plan 008 Schritt 5) ([a8294bb](https://github.com/haexhub/fwbg-agents/commit/a8294bb1fe1e47005a34b69c12e8afbfc1962674))
* **agents:** parent_run_id flow drill-down — backend (Plan 008 Schritt 5) ([81c1655](https://github.com/haexhub/fwbg-agents/commit/81c1655039962fd65f6d0df4b1f3fc7e1876c731))
* **agents:** Plan 006 — agent-run timeline + LLM-session detail (backend) ([ed50ca6](https://github.com/haexhub/fwbg-agents/commit/ed50ca60aef680dfd4867d1f6444323b73f12ef1))
* **agents:** Plan 008 timeline follow-ups (steps 1-4, 6) ([1c0f411](https://github.com/haexhub/fwbg-agents/commit/1c0f411824e99aefd3de423c33a0143e5f8ed04e))
* **agents:** Plan 008 timeline follow-ups (steps 1-4, 6) ([30c7979](https://github.com/haexhub/fwbg-agents/commit/30c7979de6ba66b56768fdd93ef8f5e08f3f2d58))
* **agents:** Plan 008 timeline follow-ups + parent_run_id drill-down (backend) → develop ([8aa0b60](https://github.com/haexhub/fwbg-agents/commit/8aa0b60e7c0cab0f8c49c73f422671679442470c))


### Bug Fixes

* **agents:** address CodeRabbit review on [#148](https://github.com/haexhub/fwbg-agents/issues/148) ([a22aea8](https://github.com/haexhub/fwbg-agents/commit/a22aea8d98092609377f7630c428568a4fe61e99))
* **agents:** address CodeRabbit review on [#151](https://github.com/haexhub/fwbg-agents/issues/151) ([417f3d8](https://github.com/haexhub/fwbg-agents/commit/417f3d8a369150f93982a9744c8ae983c6276c8c))
* **agents:** make run_instrumented generic over the agent output type ([72be882](https://github.com/haexhub/fwbg-agents/commit/72be8823d926e8ff75f33d8055f5398d109cef40))
* **agents:** mark evaluator run FAILED when the plugin does not verify ([15e6124](https://github.com/haexhub/fwbg-agents/commit/15e61243af692609272508d86bd851a32781f3d6))


### Documentation

* **plans:** mark Plan 006 DONE (steps 1-6, 8-10; 7 skipped) ([4ea84f8](https://github.com/haexhub/fwbg-agents/commit/4ea84f8852987c9bc3a82f767a210abb22f515a7))
* **plans:** mark Plan 007 DONE (reconciled with upstream agent-authored; gaps only) ([7c41a45](https://github.com/haexhub/fwbg-agents/commit/7c41a459dfb188ed045588594c24c012c043eb60))
* **plans:** remove obsolete Plan 007 spec (wrong premise) ([cebe88d](https://github.com/haexhub/fwbg-agents/commit/cebe88dcc2591f965b498ed728a54e3131b640d9))

## [0.18.0](https://github.com/haexhub/fwbg-agents/compare/v0.17.1...v0.18.0) (2026-07-10)


### Features

* **orchestrator:** enforce minimum iterations before strategy abandon ([063a120](https://github.com/haexhub/fwbg-agents/commit/063a1208d05583484e5da9e89fe4114975d5db81))
* **orchestrator:** enforce minimum iterations before strategy abandon ([3f33967](https://github.com/haexhub/fwbg-agents/commit/3f33967a1008a5b57bb85dcb2bf0c237fa80a03f))


### Bug Fixes

* **lint:** split long log strings in auto_runner to satisfy E501 ([959bd19](https://github.com/haexhub/fwbg-agents/commit/959bd193a1a60309a2e766f5660f9f40ac9e9f20))

## [0.17.1](https://github.com/haexhub/fwbg-agents/compare/v0.17.0...v0.17.1) (2026-07-10)


### Bug Fixes

* **docker:** use python3.12 runtime to match builder ([c2dc0e8](https://github.com/haexhub/fwbg-agents/commit/c2dc0e893be83ea098b94dee3cdf0dc1430cef99))
* **docker:** use python3.12 runtime to match builder ([086127a](https://github.com/haexhub/fwbg-agents/commit/086127a4da6dd8f63a15769ee8f920f89791b7ba))

## [0.17.0](https://github.com/haexhub/fwbg-agents/compare/v0.16.1...v0.17.0) (2026-07-10)


### Features

* **security:** frame web snippets as untrusted data ([dfc962b](https://github.com/haexhub/fwbg-agents/commit/dfc962b8355e9e076d4c19fd1ca16682cac580a2))
* **security:** harden LLM code path (plan 004) ([51af7ec](https://github.com/haexhub/fwbg-agents/commit/51af7ec303f97194863b21e18e4bd1f426785f17))
* **security:** harden the web→LLM→executable-code path ([59a3e3a](https://github.com/haexhub/fwbg-agents/commit/59a3e3a1d9352956c1e5ca8759b6df041449d77e))
* **security:** import allowlist in contract_check ([9cb030e](https://github.com/haexhub/fwbg-agents/commit/9cb030ee01333b3c84316cb40a6ce6b302948bfd))


### Bug Fixes

* **auto_runner:** harden add_indicator retry budget (review findings) ([1e0227b](https://github.com/haexhub/fwbg-agents/commit/1e0227b426b38e4da52acf8991abbb65e8eb0c41))
* **evaluator:** enforce finite/dtype invariants on plugin outputs ([a7335a6](https://github.com/haexhub/fwbg-agents/commit/a7335a6cb04cfbbdd38639d6d9c4205fbf5a42f1))
* **evaluator:** enforce finite/dtype invariants on plugin outputs ([b7dd0c3](https://github.com/haexhub/fwbg-agents/commit/b7dd0c3ab92f8ac2edd20e40576048fd24638dfc))
* **evaluator:** enforce finite/dtype invariants on plugin outputs ([a61b9a7](https://github.com/haexhub/fwbg-agents/commit/a61b9a73b49dce429f84d412d5b0e2e1d504c74f))
* **lifecycle:** fail-closed criteria gate + guard metric float casts ([44e16eb](https://github.com/haexhub/fwbg-agents/commit/44e16eb1c470b570da72aff6598337cb628f7788))
* **lifecycle:** fail-closed criteria gate + guard metric float casts ([2ee4f97](https://github.com/haexhub/fwbg-agents/commit/2ee4f9790d44a1f207212ba1d542125ab62e60b6))
* **lint:** clear pre-existing ruff E501/RUF002 errors blocking CI ([8dbfb98](https://github.com/haexhub/fwbg-agents/commit/8dbfb98c067e6545220529e8d3b5269efc37aa6c))
* **lint:** clear pre-existing ruff E501/RUF002 errors blocking CI ([31e17fc](https://github.com/haexhub/fwbg-agents/commit/31e17fc91074504dfddf362077eb7c1c2bee6083))
* **plans:** address code-review findings on dependency-manifest-hygiene ([d97dfd4](https://github.com/haexhub/fwbg-agents/commit/d97dfd47ed1b16d08bc1c3260080f12be5a14e37))
* **security:** address code-review findings on harden-llm-code-path ([05b02ef](https://github.com/haexhub/fwbg-agents/commit/05b02ef2489171a13d946970b58d5004cf899593))
* **types:** make `mypy src` clean ([3fbeb07](https://github.com/haexhub/fwbg-agents/commit/3fbeb071f557d3e7bcf124675998ede531eb74e3))
* **types:** restore pandas type: ignore dropped during conflict resolution ([9d3abbf](https://github.com/haexhub/fwbg-agents/commit/9d3abbf61a48a02ea593688ddb54bc21d74928ac))
* **types:** restore pandas type: ignore dropped during conflict resolution ([d7cc6b2](https://github.com/haexhub/fwbg-agents/commit/d7cc6b23aad47ce763011b789b487148ec6b9a99))
* **types:** restore pandas type: ignore[import-untyped] in plugin_evaluator ([4645d60](https://github.com/haexhub/fwbg-agents/commit/4645d6074f5b047d778704fb57bc94cf0f04fb1a))


### Documentation

* **docstrings:** add missing docstrings to reach 80%+ coverage ([6fff02f](https://github.com/haexhub/fwbg-agents/commit/6fff02fdd2bca41433d028b4e4dc8c6f52cc7f80))
* **docstrings:** add missing docstrings to reach 80%+ coverage ([b9d407f](https://github.com/haexhub/fwbg-agents/commit/b9d407f6b1e054327e010fa445b1dc034bd50b58))
* **plans:** 001 — Steps 3+4 DONE, Steps 1+2 REJECTED (SL is order-level) ([4b281e0](https://github.com/haexhub/fwbg-agents/commit/4b281e01c540dece8e8c16a37ae621cfc4056cf9))
* **plans:** mark 001 BLOCKED — stop-loss vocabulary is a maintainer decision ([ad523f3](https://github.com/haexhub/fwbg-agents/commit/ad523f3e8fd29f1d256e937e4b9b61869205c39c))
* **plans:** mark 004 DONE ([56279d5](https://github.com/haexhub/fwbg-agents/commit/56279d590960ef19d26be9c07d31a37fa1f8fc65))
* **plans:** mark 005 DONE ([958f026](https://github.com/haexhub/fwbg-agents/commit/958f026054166f79af5ae2689d9f99075a927e87))
* **plans:** mark 005 DONE ([9f355b4](https://github.com/haexhub/fwbg-agents/commit/9f355b46b331551ccd02077526de2b4c0187d4a5))

## [0.16.1](https://github.com/haexhub/fwbg-agents/compare/v0.16.0...v0.16.1) (2026-07-09)


### Bug Fixes

* **auto_runner:** retry add_indicator chain when plugin_implementer fails ([a9c1541](https://github.com/haexhub/fwbg-agents/commit/a9c1541ae81ac2b38fa9db628ac9c6585dd63e35))
* **auto_runner:** retry add_indicator chain when plugin_implementer fails ([dc84bd6](https://github.com/haexhub/fwbg-agents/commit/dc84bd6532e30a37d341575d104678db38c593de))

## [0.16.0](https://github.com/haexhub/fwbg-agents/compare/v0.15.0...v0.16.0) (2026-07-09)


### Features

* get plugin catalog from fwbg API only (single source of truth) ([93f4d57](https://github.com/haexhub/fwbg-agents/commit/93f4d57be8d31a7567c26a780f665e50b10da2ac))
* plugin catalog from fwbg API only (single source of truth) ([0d9fa5a](https://github.com/haexhub/fwbg-agents/commit/0d9fa5abbcb40230d1d473b23c05a1d665a88930))
* register VERIFIED plugins in fwbg via POST /api/plugins (Phase 3.2) ([#120](https://github.com/haexhub/fwbg-agents/issues/120)) ([e9fb796](https://github.com/haexhub/fwbg-agents/commit/e9fb79653d954224cc1a81f07d76586221fa944d))
* remove merge_with_db — fwbg API is sole plugin source (Phase 3.3) ([#121](https://github.com/haexhub/fwbg-agents/issues/121)) ([db27a72](https://github.com/haexhub/fwbg-agents/commit/db27a72abc58e26531ceba0dee6768bc5787fa63))
* **speckit:** Phase 1 — spec generator + backfill tooling ([a5c3ac0](https://github.com/haexhub/fwbg-agents/commit/a5c3ac048f2646dc7c3abd85c3b1e2517b195379))
* **speckit:** Phase 1 — spec generator + backfill tooling ([a7b6aed](https://github.com/haexhub/fwbg-agents/commit/a7b6aed309f44bcb8978f2621bea98eaa367fe0e))


### Bug Fixes

* stop guillotining long LLM calls (raise timeout, bound retries) ([f5bad6b](https://github.com/haexhub/fwbg-agents/commit/f5bad6bf48ca42662c1db20cbd5317c16e3e31bd))
* stop guillotining long LLM calls (timeout + retries) ([8ccb558](https://github.com/haexhub/fwbg-agents/commit/8ccb5580949408eb4932317abae1ab5bbbeabf42))

## [0.15.0](https://github.com/haexhub/fwbg-agents/compare/v0.14.0...v0.15.0) (2026-07-08)


### Features

* **speckit:** Phase 0 — PluginSpec, renderer, constitution ([fe2f53f](https://github.com/haexhub/fwbg-agents/commit/fe2f53fc65ba40981c5c627cd09532f8f5e9e34a))
* **speckit:** Phase 0 foundation — PluginSpec, renderer, constitution ([dec5070](https://github.com/haexhub/fwbg-agents/commit/dec5070d5dd9c549b2e77218989619d785e77a42))


### Bug Fixes

* classify API errors into clear messages ([45d75b1](https://github.com/haexhub/fwbg-agents/commit/45d75b1cc686fe314026885ffe245409367bc78c))
* classify API errors into clear messages instead of raw SDK text ([5f8c801](https://github.com/haexhub/fwbg-agents/commit/5f8c801df332951ad1d2fbc80c76aa04677726ce))
* **review:** address code-review findings on speckit Phase 0 ([1794bcb](https://github.com/haexhub/fwbg-agents/commit/1794bcb8baf66eb7e34d0e3edfda1a7371932f43))
* **review:** address code-review findings on the API error classifier ([68fc836](https://github.com/haexhub/fwbg-agents/commit/68fc836835ebf5ec0b4fe7decb7edf3baf385bb9))

## [0.14.0](https://github.com/haexhub/fwbg-agents/compare/v0.13.3...v0.14.0) (2026-07-07)


### Features

* **analyst:** family history, per-asset analysis, modify_plugins, auto plugin chain ([8298e54](https://github.com/haexhub/fwbg-agents/commit/8298e54db94e5b39e850ea7f5ab9ea57d160eb7d))
* **analyst:** family history, per-asset analysis, modify_plugins, auto plugin chain ([5754894](https://github.com/haexhub/fwbg-agents/commit/5754894b610465c2fe104ca8c77d504e1cf905a6))


### Bug Fixes

* **auto_runner:** unblock pipeline when analyst returns ChangeExit(new_exit_strategy=None) ([ba9f80b](https://github.com/haexhub/fwbg-agents/commit/ba9f80b5bececdfb5316b59dd2fa6c16d0cd448e))
* **auto_runner:** unblock pipeline when analyst returns ChangeExit(new_exit_strategy=None) ([f06ae45](https://github.com/haexhub/fwbg-agents/commit/f06ae454783afb994a87a528b2aa26d3ba41043e))

## [0.13.3](https://github.com/haexhub/fwbg-agents/compare/v0.13.2...v0.13.3) (2026-07-06)


### Bug Fixes

* **analyst:** tolerate near-miss AddIndicator enums instead of crashing ([29d1317](https://github.com/haexhub/fwbg-agents/commit/29d13176cd7625082cff5d13c809b6e00309eefc))
* **analyst:** tolerate near-miss AddIndicator enums instead of crashing ([fb71146](https://github.com/haexhub/fwbg-agents/commit/fb711469e68cb8d75018f28e006a0474c46b7356))

## [0.13.2](https://github.com/haexhub/fwbg-agents/compare/v0.13.1...v0.13.2) (2026-07-06)


### Bug Fixes

* **agents:** make research cancel actually stop the flow ([a1ddd97](https://github.com/haexhub/fwbg-agents/commit/a1ddd978aed94d2e37950fff6a2221ba3e43f7e7))
* **auto-runner:** drain backtested backlog + abandon capped + restore research cancel ([deb29cb](https://github.com/haexhub/fwbg-agents/commit/deb29cb46139c288dc44c8687df57a4b5663f75b))
* **auto-runner:** drain backtested backlog + abandon capped proposed ([f502f1e](https://github.com/haexhub/fwbg-agents/commit/f502f1ec341f1eec6ad7973fd50a9183df14f081))

## [0.13.1](https://github.com/haexhub/fwbg-agents/compare/v0.13.0...v0.13.1) (2026-07-06)


### Bug Fixes

* **agents:** stop analyst crashes, unblock research, add run cancel + hang resilience ([a6e68b4](https://github.com/haexhub/fwbg-agents/commit/a6e68b4170d0eab694efc0904a277b08f8c8b62b))
* **agents:** stop analyst crashes, unblock research, add run cancel + hang resilience ([b9d1f5f](https://github.com/haexhub/fwbg-agents/commit/b9d1f5f9357d11bd2d07025fdb749f47d20e0b6c))
* **docker:** align runtime base to python3.12 (matches builder venv) ([e8755fa](https://github.com/haexhub/fwbg-agents/commit/e8755fa34d6843ac9693135c8f19be72ca48b7a9))

## [0.13.0](https://github.com/haexhub/fwbg-agents/compare/v0.12.2...v0.13.0) (2026-07-05)


### Features

* **pipeline:** iteration-first — prefer depth over breadth in auto-runner ([92ddf35](https://github.com/haexhub/fwbg-agents/commit/92ddf35611bd37837eff0cd06959ee5f9dfa7af9))
* **pipeline:** iteration-first — prefer depth over breadth in auto-runner ([2ded03d](https://github.com/haexhub/fwbg-agents/commit/2ded03dfb08cfd3275629de3cd986118b72ea789))

## [0.12.2](https://github.com/haexhub/fwbg-agents/compare/v0.12.1...v0.12.2) (2026-07-04)


### Bug Fixes

* **translator:** validate inline pipeline params against schema options ([a64bf9d](https://github.com/haexhub/fwbg-agents/commit/a64bf9d479c0c1a9d38a0a8fd572ceeb5a40404d))
* **translator:** validate inline pipeline params against schema options ([30d9a80](https://github.com/haexhub/fwbg-agents/commit/30d9a80eb4ef240f52ac45f5072bbd557d10b509))

## [0.12.1](https://github.com/haexhub/fwbg-agents/compare/v0.12.0...v0.12.1) (2026-07-04)


### Bug Fixes

* **analyst:** guard None asset_class/strategy_family and raise output retries ([85da189](https://github.com/haexhub/fwbg-agents/commit/85da189f198e35073f7bc4b6f44ba4d4e3b94da2))
* **analyst:** guard None asset_class/strategy_family and raise output retries ([76497c2](https://github.com/haexhub/fwbg-agents/commit/76497c297d9e5a6ac1eb87625dd188dc30ef4734))
* **analyst:** use correct pydantic-ai 2.0 retries API ([4be6ea5](https://github.com/haexhub/fwbg-agents/commit/4be6ea5d983d84b2ef0a81b14a4cd1f18e56532f))

## [0.12.0](https://github.com/haexhub/fwbg-agents/compare/v0.11.0...v0.12.0) (2026-07-04)


### Features

* **auto-runner:** wire analyst + auto-reiterate after backtest ([887bd72](https://github.com/haexhub/fwbg-agents/commit/887bd72727d540e7b62c91476ccfef6a4070111d))
* **auto-runner:** wire analyst + auto-reiterate after backtest ([223f66f](https://github.com/haexhub/fwbg-agents/commit/223f66f9958c8972495d5a3ef74ab46ffd47bd13))
* **queue:** add queue_position + reorder endpoint for backtest queue ([9c48039](https://github.com/haexhub/fwbg-agents/commit/9c4803946b1dddecaaa8bf3730cd17b345fc06d2))


### Bug Fixes

* **lint:** split long analyst import lines to satisfy ruff E501 ([b9dcec9](https://github.com/haexhub/fwbg-agents/commit/b9dcec9d543e6e8e9a3bd1a0c00a41a8772bd61b))

## [0.11.0](https://github.com/haexhub/fwbg-agents/compare/v0.10.1...v0.11.0) (2026-07-04)


### Features

* **api:** expose pipeline_min_proposed via GET/PUT /runner/auto ([0236c74](https://github.com/haexhub/fwbg-agents/commit/0236c7433a95efd44665f1918232a38c7404ce22))
* **auto-runner:** decouple backtest and research loops ([ff7c6fe](https://github.com/haexhub/fwbg-agents/commit/ff7c6fe0d5b74daa67df135d53761dd32f1896fc))
* **auto-runner:** decouple backtest and research loops ([e49c95c](https://github.com/haexhub/fwbg-agents/commit/e49c95cba28df459d568038648ba0faa195aa5e9))
* decouple backtest/research loops and expose pipeline_min_proposed API ([f6c2f06](https://github.com/haexhub/fwbg-agents/commit/f6c2f06866f835e1b9787a0c1795dfe6557421e1))

## [0.10.1](https://github.com/haexhub/fwbg-agents/compare/v0.10.0...v0.10.1) (2026-07-04)


### Bug Fixes

* **auto-runner:** set started_at on pipeline-fill AgentRun ([4df50c0](https://github.com/haexhub/fwbg-agents/commit/4df50c03a860f11ff027812699057a22f357be39))
* **auto-runner:** set started_at when creating pipeline-fill AgentRun ([e3c94dc](https://github.com/haexhub/fwbg-agents/commit/e3c94dc1a8253c352841071c4c22fe8d8e207d54))
* **runner:** don't count transient network errors against retry budget ([d9bc680](https://github.com/haexhub/fwbg-agents/commit/d9bc680aa7c3d7714ac802e756f614d41ae3ea0a))
* **runner:** don't count transient network errors against retry budget ([95ecfbe](https://github.com/haexhub/fwbg-agents/commit/95ecfbead38bec8f73ee27db5794152e3cc3ca70))

## [0.10.0](https://github.com/haexhub/fwbg-agents/compare/v0.9.3...v0.10.0) (2026-07-04)


### Features

* **pipeline:** sequential researcher + auto pipeline fill ([dbbb3c8](https://github.com/haexhub/fwbg-agents/commit/dbbb3c83f29ea17e9042e4c081bc6f22e2dc2990))
* **pipeline:** sequential researcher + auto pipeline fill ([513e54e](https://github.com/haexhub/fwbg-agents/commit/513e54efaf5a5c6a5ece39d568c035776c63b3a9))


### Bug Fixes

* **lint:** store asyncio.create_task reference to prevent GC (RUF006) ([51470d4](https://github.com/haexhub/fwbg-agents/commit/51470d43c46ad66ead7ed983239217d05dda0d2b))
* **runner:** adopt active fwbg runs + wait for the single backtest slot ([6fe10de](https://github.com/haexhub/fwbg-agents/commit/6fe10dec41127db4f99d6062e31878572ab281ca))
* **runner:** adopt active fwbg runs + wait for the single backtest slot ([9905932](https://github.com/haexhub/fwbg-agents/commit/990593250096bba75e67f962a401b8f9eb881434))

## [0.9.3](https://github.com/haexhub/fwbg-agents/compare/v0.9.2...v0.9.3) (2026-07-03)


### Bug Fixes

* **runner:** survive transient fwbg outages mid-backtest + 8h backtest cap ([7f95397](https://github.com/haexhub/fwbg-agents/commit/7f953978f18048dc79451a5ed021c39ed51f7677))
* **runner:** survive transient fwbg outages mid-backtest + realistic timeout ([7a0a7b0](https://github.com/haexhub/fwbg-agents/commit/7a0a7b00983cdee7289500ac5a1344f83b855917))

## [0.9.2](https://github.com/haexhub/fwbg-agents/compare/v0.9.1...v0.9.2) (2026-07-03)


### Bug Fixes

* **db:** WAL mode + busy_timeout for SQLite — concurrent writers died with "database is locked" ([83023de](https://github.com/haexhub/fwbg-agents/commit/83023dea6740f8ef6bf86fd45d17dbf15f1d2327))
* **runs:** fail orphaned agent runs at startup + operator abandon endpoint ([f0f11f8](https://github.com/haexhub/fwbg-agents/commit/f0f11f8d6230f261680691ae619a7df778fa5b8d))
* **runs:** fail orphaned agent runs at startup + operator abandon endpoint ([18ec9dd](https://github.com/haexhub/fwbg-agents/commit/18ec9dd40486dc313fe013e8ec5407c13a666612))

## [0.9.1](https://github.com/haexhub/fwbg-agents/compare/v0.9.0...v0.9.1) (2026-07-03)


### Bug Fixes

* **docker:** stop ignoring prompts/ in the build context ([96264e6](https://github.com/haexhub/fwbg-agents/commit/96264e6d0f616a5ad666502e48fc4422d38510e5))
* **docker:** stop ignoring prompts/ in the build context ([cebd45d](https://github.com/haexhub/fwbg-agents/commit/cebd45da9b5b5a7e1530c4a53bd0d8274313d03c))

## [0.9.0](https://github.com/haexhub/fwbg-agents/compare/v0.8.0...v0.9.0) (2026-07-03)


### Features

* **catalog:** asset registry in the live catalog — data is fetched on demand ([6419c0d](https://github.com/haexhub/fwbg-agents/commit/6419c0da1c36c6243fd9f5b728c4c28bc2b73055))
* **runner:** auto mode + live timeframes + full-history awareness ([7ef9fe3](https://github.com/haexhub/fwbg-agents/commit/7ef9fe3b1c5160972a8582379d54cd0355b234a3))
* **runner:** auto mode + live timeframes + full-history awareness ([4a65a90](https://github.com/haexhub/fwbg-agents/commit/4a65a9099235b3625dc822379f460109c6f17207))


### Bug Fixes

* **agents:** ship prompts/ in the image + validate datasources against fwbg ([a678aaf](https://github.com/haexhub/fwbg-agents/commit/a678aaf25393dc0ec5409b5a54c7d3707cc7933a))
* **agents:** ship prompts/ in the image + validate datasources against fwbg ([7d15a10](https://github.com/haexhub/fwbg-agents/commit/7d15a1085af2fd65c3ef5bda52a7658cfa2c3ff9))

## [0.8.0](https://github.com/haexhub/fwbg-agents/compare/v0.7.0...v0.8.0) (2026-07-03)


### Features

* **catalog:** live plugin catalog + inline strategy composition ([0f47112](https://github.com/haexhub/fwbg-agents/commit/0f471122408ffa0125eedbcaa8dcd979556feff0))
* **catalog:** live plugin catalog + inline strategy composition ([5719df7](https://github.com/haexhub/fwbg-agents/commit/5719df747960411d5dd37b0f09438e26de65e253))
* **research:** publish translated strategies into fwbg immediately ([42be524](https://github.com/haexhub/fwbg-agents/commit/42be5243e5aad215f338648bc6f900709c1859b6))
* **research:** publish translated strategies into fwbg immediately ([2e5896f](https://github.com/haexhub/fwbg-agents/commit/2e5896fd20cf062e3f03e05ca3dc172b428a03d4))

## [0.7.0](https://github.com/haexhub/fwbg-agents/compare/v0.6.0...v0.7.0) (2026-07-02)


### Features

* **research:** add cancel + retry for stuck/failed research_flow runs ([637bd4f](https://github.com/haexhub/fwbg-agents/commit/637bd4fa8a72cc289a44990e3ac4e2eab202472f))
* **research:** cancel + retry for stuck/failed research_flow runs ([73939ef](https://github.com/haexhub/fwbg-agents/commit/73939efc7d4420944a4d1c80794550eb451b6eb7))

## [0.6.0](https://github.com/haexhub/fwbg-agents/compare/v0.5.0...v0.6.0) (2026-07-02)


### Features

* **research:** auto-start backtest after research_and_translate completes ([#31](https://github.com/haexhub/fwbg-agents/issues/31)) ([3143600](https://github.com/haexhub/fwbg-agents/commit/314360097c6f8684d30971a370c19412486bbf76))

## [0.5.0](https://github.com/haexhub/fwbg-agents/compare/v0.4.0...v0.5.0) (2026-07-02)


### Features

* **events:** real SSE event bus + researcher progress events ([a775c4f](https://github.com/haexhub/fwbg-agents/commit/a775c4f5d81734d54b973d38b749fccc4106cef7))
* **events:** real SSE event bus + researcher progress events ([412c657](https://github.com/haexhub/fwbg-agents/commit/412c657777a7eca83eaa2a723a66af9388d5fbd1))


### Bug Fixes

* **lint:** ruff E501/I001/SIM105/UP041 in events + researcher ([72271c7](https://github.com/haexhub/fwbg-agents/commit/72271c79493ea42dc93afc598562d71b3fa5a7ab))

## [0.4.0](https://github.com/haexhub/fwbg-agents/compare/v0.3.1...v0.4.0) (2026-07-02)


### Features

* **api:** expose hypothesis sources + suggested_universe on strategy endpoints ([dfa1ed6](https://github.com/haexhub/fwbg-agents/commit/dfa1ed6cf6a1e2f8d486cb49ba5dac0420ef16f1))
* **research:** strategy-first researcher (asset-agnostic + suggested_universe) ([7fe2920](https://github.com/haexhub/fwbg-agents/commit/7fe2920f0ffb921f04cd4e952d91e6e0c8b81421))
* **runner:** adaptive universe with on-demand data + fallback expansion ([4bb1daa](https://github.com/haexhub/fwbg-agents/commit/4bb1daad55b42707c91fa6a8a7734397b1a3f48d))
* **runner:** adaptive universe with on-demand data + fallback expansion ([063215a](https://github.com/haexhub/fwbg-agents/commit/063215a09599f4330a5d8f43cad20903cb720473))
* **secrets:** file-backed API-key store with runtime reads ([d39eae6](https://github.com/haexhub/fwbg-agents/commit/d39eae69f10ad3bd0c073690e9b69587219a6ec6))
* strategy-first research (asset-agnostic researcher, secrets store, hypothesis API) ([3216f58](https://github.com/haexhub/fwbg-agents/commit/3216f587140cb46c99c4afd74c6fc6d99ef547fb))


### Bug Fixes

* **lint:** fix ruff E501/UP045/F401 errors in criteria, secrets, test_secrets ([ab2755b](https://github.com/haexhub/fwbg-agents/commit/ab2755bd6b72665588b1e0cdd8096aab307a03d2))
* **lint:** satisfy ruff (ambiguous unicode, line length, unused var) ([0e8a4a1](https://github.com/haexhub/fwbg-agents/commit/0e8a4a1fa2468b30a54d4bf084cbadff508a9b67))
* **runner:** keep a rung's asset classes when its symbols have no data ([dea39dc](https://github.com/haexhub/fwbg-agents/commit/dea39dcf13e987240c979d741a41f9f17ecbd723))
* **secrets:** restrict secrets.json to owner-only (0600) ([596dcdf](https://github.com/haexhub/fwbg-agents/commit/596dcdfe043ed47b57f9631f7dda9b3fe603a528))

## [0.3.1](https://github.com/haexhub/fwbg-agents/compare/v0.3.0...v0.3.1) (2026-06-29)


### Bug Fixes

* slim agents Docker image (1.47 GB → 877 MB) ([1a836d4](https://github.com/haexhub/fwbg-agents/commit/1a836d40df5739ea4972c92377f09265c5ae912b))
* slim agents Docker image via multi-stage build + uv cache mount ([a9bcf1d](https://github.com/haexhub/fwbg-agents/commit/a9bcf1d88b24aa5dd1648c10fa7c0ad50edb25ae))

## [0.3.0](https://github.com/haexhub/fwbg-agents/compare/v0.2.0...v0.3.0) (2026-06-29)


### Features

* per-agent LLM model + persona configuration ([#13](https://github.com/haexhub/fwbg-agents/issues/13)) ([c404ed9](https://github.com/haexhub/fwbg-agents/commit/c404ed9c9be8c4dc6245d79a725828a8f204ec24))


### Bug Fixes

* surface plugin catalog + model-knowledge source fallback in prompts ([#12](https://github.com/haexhub/fwbg-agents/issues/12)) ([5b14cfe](https://github.com/haexhub/fwbg-agents/commit/5b14cfef506fd4bc0d3a425a7f9efdc4ef4ee7f3))

## [0.2.0](https://github.com/haexhub/fwbg-agents/compare/v0.1.0...v0.2.0) (2026-06-26)


### Features

* **calibrator:** compute sortino from per-trade pnls (tr_trace) ([37eb924](https://github.com/haexhub/fwbg-agents/commit/37eb924be89ddabaf37d22c1d2104b58cc47c674))
* **M1:** calibrator + criteria API ([bb6e25d](https://github.com/haexhub/fwbg-agents/commit/bb6e25d0285349eda73f436111af4ed041bb19ae))
* **M2:** lifecycle state machine + 16 tests ([17a01bf](https://github.com/haexhub/fwbg-agents/commit/17a01bf6de0feb333655cf71528a9e9b5744bf0a))
* **M2:** ORM + migration for strategy/plugin/transition ([fce0219](https://github.com/haexhub/fwbg-agents/commit/fce0219495e1ecc62fbcbe8ca62ec3814ddc6fa2))
* **M2:** read-only strategies + plugins API ([01ace83](https://github.com/haexhub/fwbg-agents/commit/01ace837b9373ac15d1c1b7356ae754153f8c3df))
* **M3:** /strategies/{id}/run + /analyze + /agents/runs endpoints ([7e7b668](https://github.com/haexhub/fwbg-agents/commit/7e7b668c25335fa67861e820e0e7d3653247bf80))
* **M3:** agent_run + llm_call tables + ORM models ([03f8e8b](https://github.com/haexhub/fwbg-agents/commit/03f8e8ba1eac4579f022a25a8934795b30180bd5))
* **M3:** Analyst agent (LLM) + recommendation schema + tests ([9751b71](https://github.com/haexhub/fwbg-agents/commit/9751b71b9a00170d7fbbc368bfb896f8e76797e9))
* **M3:** fwbg HTTP client wrapper + tests ([19021e8](https://github.com/haexhub/fwbg-agents/commit/19021e8217b06ce1635bdb035de2e77ecbd2d090))
* **M3:** POST /strategies for manual seeding ([85d91db](https://github.com/haexhub/fwbg-agents/commit/85d91db51d260b955bba9e3ffabcc1011b3eb412))
* **M3:** recommendation validator + apply ([44d19fc](https://github.com/haexhub/fwbg-agents/commit/44d19fcb0645dd9d44ec519c9ff2af3008b479fe))
* **M3:** Runner agent (deterministic) + tests ([c81e3e4](https://github.com/haexhub/fwbg-agents/commit/c81e3e41a9f3a8807a41fbb5fdf26f9d8aa86317))
* **M4:** /research/brief + /strategies/{id}/reiterate + /hypotheses API ([ed453a5](https://github.com/haexhub/fwbg-agents/commit/ed453a5f78faca693720fc1a4069a2b68c22b04f))
* **M4b:** BraveClient + FallbackSearchClient — primary/secondary search resilience ([cce8f28](https://github.com/haexhub/fwbg-agents/commit/cce8f285c1a0484dddfffff4f57b650cab467176))
* **M4b:** research_and_translate fans out RESEARCHER_FANOUT_N parallel candidates, first-valid-wins ([9b6b6fc](https://github.com/haexhub/fwbg-agents/commit/9b6b6fc27ace8b044172681ef0efa9c71f49ade3))
* **M4b:** Researcher + research_flow + API use FallbackSearchClient instead of bare TavilyClient ([4f46df5](https://github.com/haexhub/fwbg-agents/commit/4f46df55e123cf9d1561465fbcdc94ed1d0cd90e))
* **M4b:** scripts/m4b_smoke.py end-to-end fallback-search + fan-out smoke ([053b144](https://github.com/haexhub/fwbg-agents/commit/053b144141e66de7952a46bcba317e352b5fd790))
* **M4:** hypothesis schema + validator + deterministic slug generator ([4d5041a](https://github.com/haexhub/fwbg-agents/commit/4d5041a19545d2e2fecd20b507ce4c2eb7083cdf))
* **M4:** lightweight strategy.json structural validator ([3d699da](https://github.com/haexhub/fwbg-agents/commit/3d699da6111739dd763f91bf61a693dc91a954e4))
* **M4:** prior-art lookup (tag-based, no LLM) ([1e9707e](https://github.com/haexhub/fwbg-agents/commit/1e9707e474f215ecce588459953fba2efa0e349f))
* **M4:** research_flow orchestrator (Researcher → Strategy persist → Translator) ([73257f1](https://github.com/haexhub/fwbg-agents/commit/73257f14ad2d914fd044d2a9bb917d28a244075b))
* **M4:** Researcher agent (LLM + lookup_prior_art + Tavily) ([6dd3093](https://github.com/haexhub/fwbg-agents/commit/6dd3093fccd80d3c5d96d0782c723eac7f85b528))
* **M4:** Tavily client + quota tracking via llm_call ([3991d4f](https://github.com/haexhub/fwbg-agents/commit/3991d4fbd85d4fbd1288c50fd14a397f2e11eab4))
* **M4:** Translator agent — fresh mode (hypothesis to strategy.json + spec.md) ([35324cf](https://github.com/haexhub/fwbg-agents/commit/35324cff76ee04f81a60aa519d686364de940fa1))
* **M4:** Translator reiterate mode + ChangeExit.new_exit_strategy ([ed2b59a](https://github.com/haexhub/fwbg-agents/commit/ed2b59a5f9890e36fdacb3017800504f0760e191))
* **M5a:** AddIndicator recommendation + sidecar request flow ([28c4180](https://github.com/haexhub/fwbg-agents/commit/28c4180a297227963fee5b36c52014fc542d10a7))
* **M5a:** plugin discovery + DB merge catalog ([291afa6](https://github.com/haexhub/fwbg-agents/commit/291afa64b601d94434f51779cc5e567a850a33e1))
* **M5a:** PluginContract schema for contract.yaml ([0dd440b](https://github.com/haexhub/fwbg-agents/commit/0dd440bb9c27cd5d5ca68305c4d058ca8dfbc52a))
* **M5b:** migration 0004 — PluginKind extension + verification_run table ([35d41c4](https://github.com/haexhub/fwbg-agents/commit/35d41c4198e7d8e5038712289705eda331e3acff))
* **M5b:** plugin_flow API endpoints + m5 smoke end-to-end ([36aca54](https://github.com/haexhub/fwbg-agents/commit/36aca5449926345ad94baa413b13f673fb9351c9))
* **M5b:** PluginAuthor agent writes plugin.py + contract + spec and transitions to AUTHORED ([aaed198](https://github.com/haexhub/fwbg-agents/commit/aaed1981adeb08e6a0c696d8c5957b9dc87fb03b))
* **M5b:** PluginEvaluator + deterministic scenario_generators with structured error_log ([4cdc1bd](https://github.com/haexhub/fwbg-agents/commit/4cdc1bde5247d01effabdd530dcd18a0070fa664))
* **M5c:** m5c smoke end-to-end (parent → plugin → reiterate → child) ([d994766](https://github.com/haexhub/fwbg-agents/commit/d99476666414a40001eead5665b26523607e5d99))
* **M5c:** plugin_flow.reiterate_with_plugin + POST /strategies/{id}/reiterate-with-plugin ([7863ba4](https://github.com/haexhub/fwbg-agents/commit/7863ba4ce10569b6b7a2aad1b320cc02f09e7106))
* **M5c:** strategy_validator accepts plugin-slot list-fields (indicators/feature_selection/preprocessing/extra_filters) ([f55a4e3](https://github.com/haexhub/fwbg-agents/commit/f55a4e3e6f326c796cea19cc735f5347de0475a1))
* **M5c:** Translator.run_reiterate_with_plugin — deterministic slug splice into list-fields ([0acf06f](https://github.com/haexhub/fwbg-agents/commit/0acf06f44e4d43019d1a6554c61c291182b34588))
* **M5d:** author_plugin orchestrator runs Planner -&gt; Implementer with 2 AgentRuns ([1cefb65](https://github.com/haexhub/fwbg-agents/commit/1cefb65a7e8a64b51f56164cf88ffc0398c4e2ad))
* **M5d:** PluginImplementer agent with deterministic gate-loop (max_rounds=5 default) ([9a73136](https://github.com/haexhub/fwbg-agents/commit/9a73136af23a274b9c94e84557225a83f800af56))
* **M5d:** PluginPlanner agent emits PluginPlan from sidecar + parent strategy ([46b21c5](https://github.com/haexhub/fwbg-agents/commit/46b21c52c7473d997ee505ccc5331f0636182582))
* **M5d:** scripts/m5d_smoke.py end-to-end Planner -&gt; Implementer smoke ([d85ad1c](https://github.com/haexhub/fwbg-agents/commit/d85ad1c71d021c2fe5e47674e444b9efc8d14a9d))
* **M5d:** Settings fields for per-agent model + max-rounds config (Planner/Implementer split) ([24b8be0](https://github.com/haexhub/fwbg-agents/commit/24b8be0ff17b42042ce48e524696f644ca3e4af2))
* **M6a:** fwbg_paper_reader aggregates trades.jsonl + status.json + positions.json into PaperTradeSummary + PaperPositions ([be74769](https://github.com/haexhub/fwbg-agents/commit/be74769f1111f94748857002e3f7e4135b191c75))
* **M6a:** GET /strategies/{id}/paper-positions endpoint (live open positions with SL/TP for dashboard) ([954e28c](https://github.com/haexhub/fwbg-agents/commit/954e28cc0fbdb5ac792fa51308c0e071358b84e8))
* **M6a:** GET /strategies/{id}/paper-summary endpoint (dashboard-polled, no LLM) ([b5691e5](https://github.com/haexhub/fwbg-agents/commit/b5691e547bb72fda824c7b269cc4bed23e0f60c5))
* **M6a:** scripts/m6a_smoke.py — end-to-end live-telemetry smoke (summary + positions + state-guards) ([fa0a92a](https://github.com/haexhub/fwbg-agents/commit/fa0a92a4df6803c8893ae60d84b965f82ec8e27d))
* **M6a:** Strategy.paper_account_id + paper_phase_target_days columns (per-strategy account isolation + paper-phase timing) ([b765757](https://github.com/haexhub/fwbg-agents/commit/b76575787e0bef291a345a879b3dd5b5aec3fdcc))
* **M6b:** alembic 0006 — Strategy.metadata_json JSON column (generic vehicle for recommendation flags) ([e558b17](https://github.com/haexhub/fwbg-agents/commit/e558b17d782eff9a1b7469ed34e32776e2e3d7a8))
* **M6b:** hand-curated paper-criteria YAMLs for equity/forex/crypto ([33301e4](https://github.com/haexhub/fwbg-agents/commit/33301e4f8978376518c9e03a99bbde28e3a5de8d))
* **M6b:** paper_analyze orchestrator flow — Analyst → sidecar + metadata flag, no state transition ([51fdf9c](https://github.com/haexhub/fwbg-agents/commit/51fdf9cd8eed1d6db9dcecfce653d07caab61f51))
* **M6b:** paper-criteria loader + evaluator (concrete parallel to M2 backtest-criteria) ([ffa7dfb](https://github.com/haexhub/fwbg-agents/commit/ffa7dfb8451966c588050340d7e31aa4ee30f9f4))
* **M6b:** PaperAnalyst pydantic-ai agent — Promote/Abandon/Continue with hard-rule validator ([61b997c](https://github.com/haexhub/fwbg-agents/commit/61b997c14009b37c1d56748aa7830f52f860fd75))
* **M6b:** POST /strategies/{id}/paper-analyze — manual analyst trigger via BackgroundTasks ([16d8003](https://github.com/haexhub/fwbg-agents/commit/16d80036ee5895c54868e5c4cdf4a64efecafeb8))
* **M6b:** POST /strategies/{id}/promote-live — triple-gated human approval to LIVE_TRADING ([f3daf61](https://github.com/haexhub/fwbg-agents/commit/f3daf61ce9f9281a235b9a027a20b7b5b45fc1fe))
* **M6b:** scripts/m6b_smoke.py — end-to-end paper-analyst + promote-live smoke ([eccb07e](https://github.com/haexhub/fwbg-agents/commit/eccb07e7967d231e2b1917ee3f87c52ac9c65cd5))


### Bug Fixes

* **calibrator:** merge grid_details/&lt;symbol&gt;/unified_metrics.json ([b8b08d7](https://github.com/haexhub/fwbg-agents/commit/b8b08d7923e35c6dca33d74a413bdc84cf05d155))
* check out sibling fwbg repo in CI for plugin contract tests ([33e6fbe](https://github.com/haexhub/fwbg-agents/commit/33e6fbef8f27e53c3d5bc3875cfd966e97e8e8a8))
* checkout's path must stay under the workspace root ([b99394a](https://github.com/haexhub/fwbg-agents/commit/b99394a3a344bea1f23b05988bbda3f13e8821fa))
* clean up ~290 pre-existing ruff findings, re-enable CI lint step ([39d5b76](https://github.com/haexhub/fwbg-agents/commit/39d5b76c093c7ff166483b8c5df641443fd916d7))
* **M5c:** m5c smoke uses real fwbg model slug (xgboost) + self-test seeds it for catalog visibility ([22ce7e1](https://github.com/haexhub/fwbg-agents/commit/22ce7e1f3cb53d928ea693fc3f3107f3f5bf628c))
* **plugin-catalog:** map singular Plugin.kind to plural bundle-manifest category in merge_with_db ([66f5cac](https://github.com/haexhub/fwbg-agents/commit/66f5cacf9b5d0db15a4ed2b4d4452c9ab8c4fef5))
* remove invalid extra-files type from release-please config ([ad525d5](https://github.com/haexhub/fwbg-agents/commit/ad525d52d04c220e2c575d862a5d54e53c0c21d2))


### Documentation

* **M5d:** prompts/plugin_authoring.md canonical fwbg-Plugin-Konventionen ([51c16c5](https://github.com/haexhub/fwbg-agents/commit/51c16c5f71869efa36493754777f5258e1756a9e))
