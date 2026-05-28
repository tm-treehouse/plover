"""
Counter unit test classes + virtual sequences.

The vseqs run on the env's virtual sequencer and drive CounterItems on the
agent's sequencer (registered under "counter"). ``CounterBaseTest`` owns the
run-phase objection — standard pyuvm idiom — leaving the dv_lib base
untouched, exactly like ``AxilBaseTest`` in the shell testbench.
"""
from __future__ import annotations

from dv_lib import DVBaseTest, DVBaseVSeq, DVBaseSequence

from counter_env import CounterEnv, CounterEnvCfg
from counter_agent import CounterItem


# ---- Item sub-sequence ----------------------------------------------

class CounterItemSeq(DVBaseSequence):
    """Runs a pre-built list of CounterItems on the counter sequencer."""

    def __init__(self, items, name: str = "counter_item_seq") -> None:
        super().__init__(name)
        self._items = items

    async def body(self) -> None:
        for item in self._items:
            await self.start_item(item)
            await self.finish_item(item)


# ---- Virtual sequences ----------------------------------------------

class CounterSmokeVSeq(DVBaseVSeq):
    """Count up 16 cycles with enable high; result should reach 0x10."""

    def __init__(self, name: str = "CounterSmokeVSeq") -> None:
        super().__init__(name)
        self.num_cycles = 16

    async def body(self) -> None:
        await super().body()
        seqr = self.p_sequencer.sub_seqrs["counter"]
        items = [CounterItem(enable=True) for _ in range(self.num_cycles)]
        await CounterItemSeq(items).start(seqr)


class CounterClearVSeq(DVBaseVSeq):
    """Mix of enable / pause / clear cycles, exercising every transition."""

    def __init__(self, name: str = "CounterClearVSeq") -> None:
        super().__init__(name)

    async def body(self) -> None:
        await super().body()
        seqr = self.p_sequencer.sub_seqrs["counter"]
        items = (
            [CounterItem(enable=True)  for _ in range(5)]   # 0 -> 5
            + [CounterItem(enable=False) for _ in range(2)] # hold 5
            + [CounterItem(clear=True, enable=True)]        # clear wins
            + [CounterItem(enable=True)  for _ in range(3)] # 0 -> 3
        )
        await CounterItemSeq(items).start(seqr)


# ---- Base test -------------------------------------------------------

class CounterBaseTest(DVBaseTest):
    """SV equivalent: ``counter_base_test extends dv_base_test
    #(.CFG_T(counter_env_cfg), .ENV_T(counter_env));``

    Owns the run-phase objection (pyuvm idiom). dv_lib base untouched.
    """
    cfg_type = CounterEnvCfg
    env_type = CounterEnv

    def __init__(self, name: str = "CounterBaseTest", parent=None) -> None:
        super().__init__(name, parent)
        self.test_seq_s = "CounterSmokeVSeq"

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            await super().run_phase()
        finally:
            self.drop_objection()


class CounterClearTest(CounterBaseTest):
    def __init__(self, name: str = "CounterClearTest", parent=None) -> None:
        super().__init__(name, parent)
        self.test_seq_s = "CounterClearVSeq"
