"""
Copyright (C) Microsoft Corporation
SPDX-License-Identifier: MIT
"""
import unittest
import tempfile
import os
from typing import List
from pathlib import Path
from copy import deepcopy

import src.x86.x86_model as x86_model
import src.model as core_model

from src.interfaces import TestCase, Input, CTrace
from src.isa_loader import InstructionSet
from src.x86.x86_generator import X86RandomGenerator
from src.x86.x86_asm_parser import X86AsmParser
from src.config import CONF

test_path = Path(__file__).resolve()
test_dir = test_path.parent

ASM_HEADER = """
.intel_syntax noprefix
.test_case_enter:
.section .data.main
"""

ASM_THREE_LOADS = ASM_HEADER + """
mov rax, qword ptr [r14]
mov rax, qword ptr [r14 + 512]
mov rax, qword ptr [r14 + 1050]
.test_case_exit:
"""

ASM_BRANCH_AND_LOAD = ASM_HEADER + """
xor rax, rax
jnz .l1
.l0:
mov rax, qword ptr [r14]
.l1:
.test_case_exit:
"""

ASM_DOUBLE_BRANCH = ASM_HEADER + """
xor rax, rax
jnz .l1
.l0:
mov rax, qword ptr [r14]
jmp .l3
.l1:
xor rbx, rbx
jnz .l3
.l2:
mov rbx, qword ptr [r14]
.l3:
.test_case_exit:
"""

ASM_STORE_AND_LOAD = ASM_HEADER + """
mov qword ptr [r14], 2
mov rax, qword ptr [r14]
mov rax, qword ptr [r14 + rax]
.test_case_exit:
"""

ASM_FENCE = ASM_HEADER + """
xor rax, rax
jz .l1
.l0:
mov rax, qword ptr [r14]
lfence
mov rax, qword ptr [r14 + 2]
.l1:
.test_case_exit:
"""

ASM_FAULTY_ACCESS = ASM_HEADER + """
mov rax, qword ptr [r14 + rcx]
mov rax, qword ptr [r14 + rax]
mov rbx, qword ptr [r14 + rbx]
.test_case_exit:
"""

ASM_BRANCH_AND_FAULT = ASM_HEADER + """
xor rax, rax
jz .l1
.l0:
mov rax, qword ptr [r14 + rcx]
mov rax, qword ptr [r14 + rax]
.l1:
nop
.test_case_exit:
"""

ASM_FAULT_AND_BRANCH = ASM_HEADER + """
mov rax, qword ptr [r14 + rcx]
xor rbx, rbx
jz .l1
.l0:
mov rax, qword ptr [r14 + rax]
.l1:
nop
.test_case_exit:
"""

ASM_DIV_ZERO = ASM_HEADER + """
div ebx
mov rax, qword ptr [r14 + rax]
.test_case_exit:
"""

ASM_DIV_ZERO2 = ASM_HEADER + """
div rbx
mov rax, qword ptr [r14 + rax]
mov rax, qword ptr [r14 + rax]
.test_case_exit:
"""

PF_MASK = 0xfffffffffffffffe

# base addresses for calculating expected contract traces
IP0 = 0x8


class X86ModelTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # make sure that the change in the configuration does not impact the other tests
        cls.prev_conf = deepcopy(CONF)
        CONF.instruction_set = "x86-64"
        CONF.model = 'x86-unicorn'
        CONF.input_gen_seed = 10  # default
        CONF._no_generation = True

    @classmethod
    def tearDownClass(cls):
        global CONF
        CONF = cls.prev_conf

    @staticmethod
    def load_tc(asm_str: str):
        min_x86_path = test_dir / "min_x86.json"
        instruction_set = InstructionSet(min_x86_path.absolute().as_posix())

        generator = X86RandomGenerator(instruction_set, CONF.program_generator_seed)
        parser = X86AsmParser(generator)

        asm_file = tempfile.NamedTemporaryFile(delete=False)
        with open(asm_file.name, "w") as f:
            f.write(asm_str)
        tc: TestCase = parser.parse_file(asm_file.name)
        asm_file.close()
        os.unlink(asm_file.name)
        return tc

    def get_traces(self, model, asm_str, inputs, nesting=1, pte_mask: int = 0) -> List[CTrace]:
        tc = self.load_tc(asm_str)
        tc.actors["main"].data_properties = pte_mask
        model.load_test_case(tc)
        ctraces = model.trace_test_case(inputs, nesting)
        return ctraces

    def test_gpr_tracer(self):
        mem_base, code_base = 0x1000000, 0x8000
        tracer = core_model.CTTracer()
        model = x86_model.X86UnicornSeq(mem_base, code_base, tracer, True)
        input_ = Input()
        input_[0]['main'][0] = 0
        input_[0]['main'][1] = 1
        for i in range(0, 7):
            input_[0]['gpr'][i] = 2
        _ = self.get_traces(model, ASM_STORE_AND_LOAD, [input_])
        full_trace = model.tracer.get_contract_trace_full()
        expected_trace = [1 << 48, 2, 2, 2, 2, 2]
        self.assertEqual(full_trace, expected_trace)

    def test_l1d_seq(self):
        model = x86_model.X86UnicornSeq(0x1000000, 0x8000, core_model.L1DTracer())
        ctraces = self.get_traces(model, ASM_THREE_LOADS, [Input()])
        expected_trace = [(1 << 63) + (1 << 55) + (1 << 47)]
        self.assertEqual(ctraces[0].raw, expected_trace)

    def test_ct_seq(self):
        model = x86_model.X86UnicornSeq(0x1000000, 0x8000, core_model.CTTracer())
        ctraces = self.get_traces(model, ASM_BRANCH_AND_LOAD, [Input()])
        expected_trace = [IP0, IP0 + 3, IP0 + 5, 0, IP0 + 8]
        # print([hex(x - model.code_start) for x in model.tracer.get_contract_trace_full()])
        self.assertEqual(ctraces[0].raw, expected_trace)

    def test_ctr_seq(self):
        model = x86_model.X86UnicornSeq(0x1000000, 0x8000, core_model.CTRTracer())
        input_ = Input()
        for i in range(0, 7):
            input_[0]['gpr'][i] = 2
        ctraces = self.get_traces(model, ASM_BRANCH_AND_LOAD, [input_])
        # print([hex(x - model.code_start) for x in model.tracer.get_contract_trace_full()])
        expected_trace = [
            2, 2, 2, 2, 2, 2, 2, model.stack_base - model.sandbox_base, IP0, IP0 + 3, IP0 + 5, 0,
            IP0 + 8
        ]
        self.assertEqual(ctraces[0].raw, expected_trace)

    def test_arch_seq(self):
        model = x86_model.X86UnicornSeq(0x1000000, 0x8000, core_model.ArchTracer())
        input_ = Input()
        input_[0]['main'][0] = 1
        for i in range(0, 7):
            input_[0]['gpr'][i] = 2
        ctraces = self.get_traces(model, ASM_BRANCH_AND_LOAD, [input_])
        # print([hex(x - model.code_start) for x in model.tracer.get_contract_trace_full()])
        expected_trace = [
            2, 2, 2, 2, 2, 2, 2, model.stack_base - model.sandbox_base, IP0, IP0 + 3, IP0 + 5, 1, 0,
            IP0 + 8
        ]
        self.assertEqual(ctraces[0].raw, expected_trace)

    def test_ct_cond(self):
        model = x86_model.X86UnicornCond(0x1000000, 0x8000, core_model.CTTracer())
        ctraces = self.get_traces(model, ASM_BRANCH_AND_LOAD, [Input()])
        # print([hex(x - model.code_start) for x in model.tracer.get_contract_trace_full()])
        expected_trace = [IP0, IP0 + 3, IP0 + 8, IP0 + 5, 0, IP0 + 8]
        self.assertEqual(ctraces[0].raw, expected_trace)

    def test_ct_cond_double(self):
        model = x86_model.X86UnicornCond(0x1000000, 0x8000, core_model.CTTracer())
        ctraces = self.get_traces(model, ASM_DOUBLE_BRANCH, [Input()], nesting=2)
        # print([hex(x - model.code_start) for x in model.tracer.get_contract_trace_full()])
        expected_trace = [
            IP0,  # XOR rax, rax
            IP0 + 3,  # JNZ .l1
            IP0 + 10,  # XOR rbx, rbx
            IP0 + 13,  # JNZ .l3
            IP0 + 18,
            # rollback inner speculation
            IP0 + 15,
            0,  # MOV RBX, qword ptr [r14]
            IP0 + 18,
            # rollback outer speculation
            IP0 + 5,
            0,  # MOV RAX, qword ptr [r14]
            IP0 + 8,  # JMP .l3
            IP0 + 18,
        ]
        self.assertEqual(ctraces[0].raw, expected_trace)

    @unittest.skip("under construction")
    def test_ct_bpas(self):
        model = x86_model.X86UnicornBpas(0x1000000, 0x8000, core_model.CTTracer())
        input_ = Input()
        input_['main'][0] = 1
        ctraces = self.get_traces(model, ASM_STORE_AND_LOAD, [input_])
        expected_trace = [0, 0, 7, 0, 10, 1, 14, 7, 0, 10, 2, 14]
        self.assertEqual(ctraces[0].raw, expected_trace)

    def test_rollback_on_fence(self):
        model = x86_model.X86UnicornCond(0x1000000, 0x8000, core_model.MemoryTracer())
        ctraces = self.get_traces(model, ASM_FENCE, [Input()])
        expected_trace = [0]
        self.assertEqual(ctraces[0].raw, expected_trace)

    def test_fault_handling(self):
        model = x86_model.X86UnicornSeq(0x1000000, 0x8000, core_model.CTTracer())
        model.handled_faults.update([12, 13])
        input_ = Input()
        input_[0]['main'][0] = 1
        input_[0]['gpr'][2] = 4096
        ctraces = self.get_traces(model, ASM_FAULTY_ACCESS, [input_], pte_mask=PF_MASK)
        expected_trace = [IP0, 4096, 4088]
        self.assertEqual(ctraces[0].raw, expected_trace)

    def test_ct_nullinj(self):
        model = x86_model.X86UnicornNullAssist(0x1000000, 0x8000, core_model.CTTracer())
        model.rw_protect = True
        model.handled_faults.update([12, 13])
        input_ = Input()
        for i in range(0, 7):
            input_[0]['gpr'][i] = 2
        input_[0]['gpr'][2] = 4096
        input_[0]['faulty'][0] = 3
        ctraces = self.get_traces(model, ASM_FAULTY_ACCESS, [input_], pte_mask=PF_MASK)
        # print([hex(x - model.code_start) for x in model.tracer.get_contract_trace_full()])
        expected_trace = [
            IP0, 4096, 4088,  # fault
            IP0, 4096,  # speculative injection
            IP0 + 4,  # speculatively start executing the next instr
            IP0 + 4, 0,  # re-execute the instruction after setting the permissions
            IP0 + 8, 2, IP0 + 12,  # speculatively execute the last instruction and rollback
            IP0, 4096, IP0 + 4, 3, IP0 + 8, 2,  # after rollback
            IP0 + 12,
            # terminate after rollback
        ]   # yapf: disable
        # on newer versions of Unicorn, the instruction may
        # not be re-executed after changing permissions
        # hence, an alternative trace would be
        expected_trace2 = [0, 4096, 4088, 0, 4096, 4, 0, 8, 2, 0, 4096, 4, 3, 8, 2]
        self.assertIn(ctraces[0].raw, [expected_trace, expected_trace2])

    def test_ct_nullinj_term(self):
        model = x86_model.X86UnicornNull(0x1000000, 0x8000, core_model.CTTracer())
        model.handled_faults.update([12, 13])
        input_ = Input()
        for i in range(0, 7):
            input_[0]['gpr'][i] = 2
        input_[0]['gpr'][2] = 4096
        input_[0]['main'][0] = 1
        input_[0]['faulty'][0] = 3
        # model.LOG.dbg_model = not model.LOG.dbg_model
        ctraces = self.get_traces(model, ASM_FAULTY_ACCESS, [input_], pte_mask=PF_MASK)
        # print([hex(x - model.code_start) for x in model.tracer.get_contract_trace_full()])
        # model.LOG.dbg_model = not model.LOG.dbg_model
        expected_trace = [
            IP0, 4096, 4088,  # fault
            IP0, 4096,  # speculative injection
            IP0 + 4,  # speculatively start executing the next instr
            IP0 + 4, 0,  # re-execute the instruction after setting the permissions
            IP0 + 8, 2, IP0 + 12,  # speculatively execute the last instruction and rollback
            # terminate after rollback
        ]   # yapf: disable
        # on newer versions of Unicorn, the instruction may
        # not be re-executed after changing permissions
        # hence, an alternative trace would be
        expected_trace2 = [0, 4096, 4088, 0, 4096, 4, 0, 8, 2]
        self.assertIn(ctraces[0].raw, [expected_trace, expected_trace2])

    def test_ct_deh(self):
        model = x86_model.X86UnicornDEH(0x1000000, 0x8000, core_model.CTTracer())
        model.handled_faults.update([12, 13])
        input_ = Input()
        for i in range(0, 7):
            input_[0]['gpr'][i] = 2
        input_[0]['gpr'][2] = 4096
        input_[0]['main'][0] = 1
        input_[0]['faulty'][0] = 3
        ctraces = self.get_traces(model, ASM_FAULTY_ACCESS, [input_], pte_mask=PF_MASK)
        # print([hex(x - model.code_start) for x in model.tracer.get_contract_trace_full()])
        expected_trace = [
            IP0, 4096, 4088,  # faulty load
            IP0 + 4,  # next load is dependent - do not execute the mem access
            IP0 + 8, 2, IP0 + 12,  # speculatively execute the last instruction and rollback
            # terminate after rollback
        ]   # yapf: disable
        self.assertEqual(ctraces[0].raw, expected_trace)

    def test_ct_meltdown(self):
        model = x86_model.X86Meltdown(0x1000000, 0x8000, core_model.CTTracer())
        model.handled_faults.update([12, 13])
        input_ = Input()
        for i in range(0, 7):
            input_[0]['gpr'][i] = 2
        input_[0]['gpr'][2] = 4096
        input_[0]['faulty'][0] = 3
        ctraces = self.get_traces(model, ASM_FAULTY_ACCESS, [input_], pte_mask=PF_MASK)
        expected_trace = [
            IP0, 4096, 4088,  # faulty load
            IP0, 4096,  # speculative injection
            IP0 + 4, 3,  # next load is dependent - do not execute the mem access
            IP0 + 8, 2,  # speculatively execute the last instruction and rollback
            IP0 + 12,
            # terminate after rollback
        ]   # yapf: disable
        self.assertEqual(ctraces[0].raw, expected_trace)

    @unittest.skip("under construction")
    def test_ct_meltdown_double(self):
        model = x86_model.X86Meltdown(0x1000000, 0x8000, core_model.CTTracer())
        model.handled_faults.update([12, 13])
        input_ = Input()
        for i in range(0, 7):
            input_[0]['gpr'][i] = 2
        input_[0]['gpr'][2] = 4096
        input_[0]['faulty'][0] = 4097
        ctraces = self.get_traces(model, ASM_FAULTY_ACCESS, [input_], nesting=2, pte_mask=PF_MASK)
        expected_trace = hash(
            tuple([
                0, 4096, 4088,  # fault
                0, 4096,  # speculative injection
                4, 4097,  # second fault
                # no second speculative injection, fault just ignored
                8, 2, 12,  # speculatively execute the last instruction and rollback
                # terminate after rollback
            ]))   # yapf: disable
        self.assertEqual(ctraces[0].raw, expected_trace)

    @unittest.skip("under construction")
    def test_ct_branch_meltdown(self):
        model = x86_model.X86CondMeltdown(0x1000000, 0x8000, core_model.CTTracer())
        model.handled_faults.update([12, 13])
        input_ = Input()
        for i in range(0, 7):
            input_[0]['gpr'][i] = 2
        input_[0]['gpr'][2] = 4096
        input_[0]['faulty'][0] = 3
        ctraces = self.get_traces(
            model, ASM_BRANCH_AND_FAULT, [input_], nesting=2, pte_mask=PF_MASK)
        expected_trace = [
            0,
            3,  # speculatively do not jump
            5, 4096, 4088,  # fault while speculating
            5, 4096,  # speculative injection
            9, 3,  # leak [4096]
            13, 14,  # last instruction of speculation caused by exception, rollback
            13, 14,  # execution of correct branch
        ]   # yapf: disable
        self.assertEqual(ctraces[0].raw, expected_trace)

    @unittest.skip("under construction")
    def test_ct_meltdown_branch(self):
        model = x86_model.X86CondMeltdown(0x1000000, 0x8000, core_model.CTTracer())
        model.handled_faults.update([12, 13])
        input_ = Input()
        for i in range(0, 7):
            input_[0]['gpr'][i] = 2
        input_[0]['gpr'][2] = 4096
        input_[0]['faulty'][0] = 3
        # model.LOG.dbg_model = True
        ctraces = self.get_traces(
            model, ASM_FAULT_AND_BRANCH, [input_], nesting=2, pte_mask=PF_MASK)
        expected_trace = [
            0,
            4096,
            4088,  # faulty access
            0,
            4096,  # speculative injection
            4,  # xor
            7,  # speculatively do not jump
            9,
            3,  # leak [4096]
            13,
            14,  # end of branch speculation, rollback
            13,
            14,  # execution of correct branch
            # end of speculation after exception, rollback and terminate
        ]
        # print(expected_trace_tmp)
        # print(model.tracer.get_contract_trace_full())
        # model.LOG.dbg_model = False
        self.assertEqual(ctraces[0].raw, expected_trace)

    def test_ct_vsops(self):
        model = x86_model.x86UnicornVspecOpsDIV(0x1000000, 0x8000, core_model.CTTracer())
        model.handled_faults.add(21)
        input_ = Input()
        input_[0]['gpr'][0] = 0  # rax
        input_[0]['gpr'][1] = 0  # rbx
        input_[0]['gpr'][3] = 0  # rdx
        input_[0]['main'][0] = 0

        ctraces = self.get_traces(model, ASM_DIV_ZERO2, [input_])
        # print([hex(x - model.code_start) for x in model.tracer.get_contract_trace_full()])
        hash_of_operands = hash((
            (IP0, 35, 0),  # rax
            (IP0, 37, 0),  # rbx
            (IP0, 40, 0),  # rdx
            (IP0 + 3, 112, 0x1000000)  # r14
        ))
        hash_of_input = hash(((0, 0, hash(input_)),))
        expected_trace_full = [
            IP0, 0xff8,  # fault
            IP0 + 3, hash_of_operands,  # first mem access exposes the hash of the div operands
            IP0 + 7, hash_of_input,  # next mem access exposes the hash of the whole input
            IP0 + 11,  # terminate after rollback
        ]  # yapf: disable
        self.assertEqual(ctraces[0].raw, expected_trace_full)

    def test_ct_vsall(self):
        model = x86_model.x86UnicornVspecAllDIV(0x1000000, 0x8000, core_model.CTTracer())
        model.handled_faults.add(21)
        input_ = Input()
        input_[0]['gpr'][0] = 2  # rax
        input_[0]['gpr'][1] = 0  # rbx
        input_[0]['gpr'][3] = 0  # rdx

        ctraces = self.get_traces(model, ASM_DIV_ZERO, [input_])
        # print([hex(x - model.code_start) for x in model.tracer.get_contract_trace_full()])
        hash_of_input = hash(((0, 0, hash(input_)),))
        expected_trace_full = [
            IP0, 0xff8,  # fault
            IP0 + 2, hash_of_input, IP0 + 6,  # mem access exposes the input hash
            # terminate after rollback
        ]  # yapf: disable
        self.assertEqual(ctraces[0].raw, expected_trace_full)


if __name__ == '__main__':
    unittest.main()
