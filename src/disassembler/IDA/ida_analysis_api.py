import idautils
import idaapi
import idc
import sark
from config.utils   import *
from hashlib        import md5

class AnalyzerIDA(object):
    """Logic instance for the IDA disassembler API. Contains the heart of Karta's canonical representation.

    Note:
        Contains specific Karta logic.

    Attributes:
        _disas (disassembler): disassembler layer instance
    """

    def __init__(self, disas):
        """Basic Ctor
        
        Args:
            disas (disassembler): disassembler layer instance
        """
        self._disas = disas

    def funcNameInner(self, raw_func_name):
        """Returns the name of the function (including windows name fixes)
        
        Args:
            raw_func_name (str): raw string func name

        Return Value:
            The actual (wanted) name of the wanted function
        """
        base_name = raw_func_name
        # check for the libc edge case
        if isWindows() and (not isMatching()) and base_name.startswith("__imp_"):
            base_name = base_name[len("__imp_") : ]
        if isWindows() and (not isMatching()) and base_name.startswith("_") :
            return base_name[1:]
        else:
            return base_name

    def funcNameEA(self, func_ea):
        """Returns the name of the function that was defined in the given address (including windows name fixes)
        
        Args:
            func_ea (int): effective address of the wanted function

        Return Value:
            The actual (wanted) name of the wanted function
        """
        func = self._disas.funcAt(func_ea)
        if func is not None:
            return self.funcNameInner(func.name)
        return self.funcNameInner(self._disas.nameAt(func_ea))

    def analyzeFunctionGraph(self, func_ea, src_mode) :
        """Analyzes the flow graph of a given function, generating a call-order mapping
        
        Args:
            func_ea (int): effective address of the wanted function
            src_mode (bool): True iff analyzing a self-compiled source file, otherwise analyzing a binary function

        Return Value:
            A dictionary representing the the list of function calls that lead to a specific function call: call ==> list of preceding calls
        """
        block_to_ref   = {}
        ref_to_block   = {}
        ref_to_call    = {}
        block_to_reach = {}
        call_to_reach  = {}
        # 1st scan, build up the mappings - O(N) time, O(k) storage
        func = sark.Function(func_ea)
        func_start = func.startEA
        flow = idaapi.FlowChart(func.func_t)
        for block in flow :
            block_to_reach[block.start_ea] = set()
            try :
                block_lines = sark.CodeBlock(block.start_ea).lines
            except :
                continue
            for line in block_lines :
                instr_pos = line.ea
                call_candidates = set()
                # Data Refs (strings, fptrs)
                for ref in line.drefs_from :
                    # Check for a string (finds un-analyzed strings too)
                    str_const = self._disas.stringAt(ref)
                    if str_const is not None and len(str_const) >= MIN_STR_SIZE :
                        continue
                    # Check for an fptr
                    try :
                        call_candidates.add(sark.Function(ref).startEA)
                    except :
                        continue
                # Check for a function call
                for cref in line.crefs_from :
                    try :
                        if (cref == func_start and line.insn.is_call) or sark.Function(cref).startEA != func_start :
                            call_candidates.add(sark.Function(cref).startEA)
                    except Exception, e:
                        continue
                # handle each ref
                for ref in call_candidates :
                    call = sark.Function(ref)
                    # record the call
                    if block.start_ea not in block_to_ref :
                        block_to_ref[block.start_ea] = set()
                    block_to_ref[block.start_ea].add(instr_pos)
                    ref_to_block[instr_pos] = block
                    ref_to_call[instr_pos] = self.funcNameInner(call.name) if src_mode else call.startEA

        # 2nd scan, start from each reference, and propagate till the end - O(kN), E(N) time, O(N) storage
        sorted_refs = ref_to_block.keys()
        sorted_refs.sort()
        for ref in sorted_refs :
            start_block = ref_to_block[ref]
            working_set = set([ref])
            # we distinguish between refs even on the same block, no need to search for them because we scan using sorted_refs
            # mark the start block
            block_to_reach[start_block.start_ea].add(ref)
            # check if we can stop now
            if len(block_to_ref[start_block.start_ea]) > 1 and ref != max(block_to_ref[start_block.start_ea]) :
                continue
            # carry on the tasks that were leftover by previous references
            working_set.update(block_to_reach[start_block.start_ea])
            # build a list of BFS nodes
            search_list = map(lambda x : (x, set(working_set)), start_block.succs())
            seen_blocks = set()
            # BFS Scan - until the list is empty
            while len(search_list) > 0 :
                new_search_list = []
                for cur_block, working_set in search_list :
                    # check for loops
                    if cur_block.start_ea in seen_blocks and len(block_to_reach[cur_block.start_ea].difference(working_set)) == 0 :
                        continue
                    # mark as seen
                    seen_blocks.add(cur_block.start_ea)
                    # always mark it
                    block_to_reach[cur_block.start_ea].update(working_set)
                    # if reached a starting block of a lesser reference, tell him to keep on for us
                    if cur_block.start_ea in block_to_ref and max(block_to_ref[cur_block.start_ea]) > cur_block.start_ea :
                        # we can stop :)
                        continue
                    # learn, and keep going
                    else :
                        working_set.update(block_to_reach[cur_block.start_ea])
                        new_search_list += map(lambda x : (x, set(working_set)), cur_block.succs())
                search_list = new_search_list

        # 3rd scan, sum up the results - O(k) time, O(k*k) storage
        for ref in ref_to_block.keys() :
            reachable_from = block_to_reach[ref_to_block[ref].start_ea]
            # add a filter to prevent collisions from the same block
            reachable_from = reachable_from.difference(filter(lambda x : x > ref, block_to_ref[ref_to_block[ref].start_ea]))
            if ref_to_call[ref] not in call_to_reach :
                call_to_reach[ref_to_call[ref]] = []
            current_record = set(filter(lambda x : x != ref_to_call[ref], map(lambda x : ref_to_call[x], reachable_from)))
            if current_record not in call_to_reach[ref_to_call[ref]] :
                call_to_reach[ref_to_call[ref]].append(list(current_record))

        # return the results
        return call_to_reach

    def analyzeFunction(self, func_ea, src_mode) :
        """Analyze a given function, and creates a canonical representation for it
        
        Args:
            func_ea (int): effective address of the wanted function
            src_mode (bool): True iff analyzing a self-compiled source file, otherwise analyzing a binary function

        Return Value:
            FunctionContext object representing the analyzed function
        """
        func = sark.Function(func_ea)
        context = FunctionContext(self.funcNameInner(func.name), func_ea)
        
        func_start = func.startEA
        instr_count = 0
        call_candidates = set()
        code_hash = md5()
        for line in func.lines :
            instr_count += 1
            # Numeric Constants
            data_refs = list(line.drefs_from)
            for oper in filter(lambda x : x.type.is_imm, line.insn.operands) :
                if oper.imm not in data_refs :
                    context.recordConst(oper.imm)
            # Data Refs (strings, fptrs)
            for ref in data_refs :
                # Check for a string (finds un-analyzed strings too)
                str_const = self._disas.stringAt(ref)
                if str_const is not None and len(str_const) >= MIN_STR_SIZE :
                    context.recordString(str_const)
                    continue
                # Check for an fptr
                called_func = self._disas.funcAt(ref)
                if called_func is not None:
                    call_candidates.add(self._disas.funcStart(called_func))
                elif src_mode:
                    call_candidates.add(ref)
                    continue
            # Code Refs (calls and unknowns)
            for cref in line.crefs_from :
                called_func = self._disas.funcAt(cref)
                if called_func is None:
                    continue
                called_func_start = self._disas.funcStart(called_func)
                if (cref == func_start and line.insn.is_call) or called_func_start != func_start :
                    call_candidates.add(called_func_start)
            # in binary mode don't let the call_candidates expand too much
            if not src_mode :
                map(lambda x : context.recordCall(x), call_candidates)
                call_candidates = set()
            # hash the instruction (only in source mode)
            else:
                # two cases:
                # 1. No linker fixups, hash the binary - easy case
                # 2. Linker fixups, hash the text (includes the symbol name that the linker will use too)
                has_fixups = False
                # data variables
                for dref in line.drefs_from:
                    if sark.Line(dref).name in self._disas.exports() :
                        has_fixups = True
                        break
                # external code functions
                if not has_fixups:
                    for cref in line.crefs_from:
                        if sark.Line(cref).name in self._disas.exports() :
                            has_fixups = True
                            break
                # case #2
                if has_fixups :
                    code_hash.update(line.disasm)
                # case #1
                else:
                    code_hash.update(line.bytes)

        # check all the call candidates together
        if src_mode :
            for candidate in call_candidates :
                ref_func = None
                called_func = self._disas.funcAt(candidate)
                if called_func is not None:
                    ref_func = self._disas.funcName(called_func)
                    risky = False
                else:
                    ref_func = self._disas.nameAt(candidate)
                    risky = True
                # check if known or unknown
                if sark.Line(candidate).disasm.startswith("extrn ") :
                    context.recordUnknown(ref_func, is_fptr = risky)
                elif not risky :
                    context.recordCall(ref_func)
            # set the function's hash
            context.setHash(code_hash.hexdigest())

        context.setFrame(func.frame_size)
        context.setInstrCount(instr_count)

        # Now, record the code blocks
        flow = idaapi.FlowChart(func.func_t)
        for block in flow :
            try :
                context.recordBlock(len(list(sark.CodeBlock(block.start_ea).lines)))
            except :
                # happens with code outside of a function
                continue
        context._blocks.sort(reverse = True)

        # Now add the flow analysis
        context.setCallOrder(self.analyzeFunctionGraph(func_ea, src_mode))

        return context

    def searchIslands(self, func_ea, range_start, range_end) :
        """Search a given function for "Islands" from a specific code range
        
        Args:
            func_ea (int): effective address of the wanted function
            range_start (int): effective address of the start of the island range
            range_end (int): effective address of the end of the island range

        Return Value:
            Orderred list of code blocks for the found island, or None if found nothing
        """
        island_guess = None
        func = sark.Function(func_ea)
        flow = idaapi.FlowChart(func.func_t)
        for block in flow :
            if range_start <= block.start_ea and block.end_ea <= range_end :
                if island_guess is None or block.start_ea < island_guess.start_ea :
                    island_guess = block
        # quit if found nothing
        if island_guess is None :
            return None
        # make sure that the island is indeed an island, and not a well known function
        if sark.Function(island_guess.start_ea).startEA == island_guess.start_ea :
            return None
        # find the contained flow, that island_guess is the start of
        island_blocks = []
        candidate_list = [island_guess]
        while len(candidate_list) != 0 :
            new_candidate_list = []
            for candidate_block in candidate_list :
                if candidate_block in island_blocks :
                    continue
                island_blocks.append(candidate_block)
                new_candidate_list += filter(lambda succs : range_start <= succs.start_ea and succs.end_ea <= range_end, candidate_block.succs())
            candidate_list = new_candidate_list
        # return the results  
        return island_blocks

    def analyzeIslandFunction(blocks) :
        """Analyze a given island function, and creates a canonical representation for it
        
        Args:
            blocks (list): orderred list of code blocks (as returned from searchIslands())

        Return Value:
            IslandContext object representing the analyzed island
        """
        island_start = blocks[0].start_ea
        func = sark.Function(island_start)
        func_start = func.startEA
        context = IslandContext(self.funcNameInner(func.name), island_start)
        for block in blocks :
            for line in sark.CodeBlock(block.start_ea).lines :
                # Numeric Constants
                data_refs = list(line.drefs_from)
                for oper in filter(lambda x : x.type.is_imm, line.insn.operands) :
                    if oper.imm not in data_refs :
                        context.recordConst(oper.imm)
                        context._const_ranks[oper.imm] = rankConst(oper.imm, None)
                # Data Refs (strings, fptrs)
                for ref in data_refs :
                    # Check for a string (finds un-analyzed strings too)
                    str_const = self._disas.stringAt(ref)
                    if str_const is not None and len(str_const) >= MIN_STR_SIZE :
                        context.recordString(str_const)
                        continue
                    # Check for an fptr
                    called_func = self._disas.funcAt(ref)
                    if called_func is not None:
                        context.recordCall(self._disas.funcStart(called_func))
                # Code Refs (calls)
                for cref in line.crefs_from :
                    called_func = self._disas.funcAt(ref)
                    if called_func is None:
                        continue
                    called_func_start = self._disas.funcStart(called_func)
                    if (cref == func_start and line.insn.is_call) or called_func_start != func_start :
                        context.recordCall(called_func_start)

        return context

    def locateAnchorConsts(self, func_ea, const_set) :
        """Analyzes the function in search for specific immediate numerics

        Args:
            func_ea (int): effective address of the analyzed function
            const_set (set): set of numeric consts to search for as immediate values
        
        Return Value :
            a set that contains the matched immediate value, an empty set if found none)
        """
        results = set()
        for line in sark.Function(func_ea).lines :
            # Numeric Constants
            data_refs = list(line.drefs_from)
            for oper in filter(lambda x : x.type.is_imm, line.insn.operands) :
                if oper.imm in const_set and oper.imm not in data_refs :
                    results.add(oper.imm)
        return results