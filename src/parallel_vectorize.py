'''
This file implements the code-generator for parallel-vectorize.

ParallelUFunc is the platform independent base class for generating
the thread dispatcher.  This thread dispatcher launches threads
that execute the generated function of UFuncCore.
UFuncCore is subclassed to specialize for the input/output types.
The actual workload is invoked inside the function generated by UFuncCore.
UFuncCore also defines a work-stealing mechanism that allows idle threads
to steal works from other threads.
'''

from llvm.core import *
from llvm.passes import *
from llvm.ee import TargetMachine

from llvm_cbuilder import *
import llvm_cbuilder.shortnames as C

import numpy as np

import sys

class WorkQueue(CStruct):
    '''structure for workqueue for parallel-ufunc.
    '''

    _fields_ = [
        ('next', C.intp),  # next index of work item
        ('last', C.intp),  # last index of work item (exlusive)
        ('lock', C.int),   # for locking the workqueue
    ]


    def Lock(self):
        '''inline the lock procedure.
        '''
        with self.parent.loop() as loop:
            with loop.condition() as setcond:
                unlocked = self.parent.constant(self.lock.type, 0)
                locked = self.parent.constant(self.lock.type, 1)

                res = self.lock.reference().atomic_cmpxchg(unlocked, locked,
                                               ordering='acquire')
                setcond( res != unlocked )

            with loop.body():
                pass

    def Unlock(self):
        '''inline the unlock procedure.
        '''
        unlocked = self.parent.constant(self.lock.type, 0)
        locked = self.parent.constant(self.lock.type, 1)

        res = self.lock.reference().atomic_cmpxchg(locked, unlocked,
                                                   ordering='release')

        with self.parent.ifelse( res != locked ) as ifelse:
            with ifelse.then():
                # This shall kill the program
                self.parent.unreachable()


class ContextCommon(CStruct):
    '''structure for thread-shared context information in parallel-ufunc.
    '''
    _fields_ = [
        # loop ufunc args
        ('args',        C.pointer(C.char_p)),
        ('dimensions',  C.pointer(C.intp)),
        ('steps',       C.pointer(C.intp)),
        ('data',        C.void_p),
        # specifics for work queues
        ('func',        C.void_p),
        ('num_thread',  C.int),
        ('workqueues',  C.pointer(WorkQueue.llvm_type())),
    ]

class Context(CStruct):
    '''structure for thread-specific context information in parallel-ufunc.
    '''
    _fields_ = [
        ('common',    C.pointer(ContextCommon.llvm_type())),
        ('id',        C.int),
        ('completed', C.intp),
    ]

class ParallelUFunc(CDefinition):
    '''the generic parallel vectorize mechanism

    Can be specialized to the maximum number of threads on the platform.


    Platform dependent threading function is implemented in

    def _dispatch_worker(self, worker, contexts, num_thread):
        ...

    which should be implemented in subclass or mixin.
    '''

    _argtys_ = [
        ('func',       C.void_p),
        ('worker',     C.void_p),
        ('args',       C.pointer(C.char_p)),
        ('dimensions', C.pointer(C.intp)),
        ('steps',      C.pointer(C.intp)),
        ('data',       C.void_p),
    ]

    @classmethod
    def specialize(cls, num_thread):
        '''specialize to the maximum # of thread
        '''
        cls._name_ = 'parallel_ufunc_%d' % num_thread
        cls.ThreadCount = num_thread

    def body(self, func, worker, args, dimensions, steps, data):
        # Setup variables
        ThreadCount = self.ThreadCount
        common = self.var(ContextCommon, name='common')
        workqueues = self.array(WorkQueue, ThreadCount, name='workqueues')
        contexts = self.array(Context, ThreadCount, name='contexts')

        num_thread = self.var(C.int, ThreadCount, name='num_thread')

        # Initialize ContextCommon
        common.args.assign(args)
        common.dimensions.assign(dimensions)
        common.steps.assign(steps)
        common.data.assign(data)
        common.func.assign(func)
        common.num_thread.assign(num_thread.cast(C.int))
        common.workqueues.assign(workqueues.reference())

        # Determine chunksize, initial count of work-items per thread.
        # If total_work >= num_thread, equally divide the works.
        # If total_work % num_thread != 0, the last thread does all remaining works.
        # If total_work < num_thread, each thread does one work,
        # and set num_thread to total_work
        N = dimensions[0]
        ChunkSize = self.var_copy(N / num_thread.cast(N.type))
        ChunkSize_NULL = self.constant_null(ChunkSize.type)
        with self.ifelse(ChunkSize == ChunkSize_NULL) as ifelse:
            with ifelse.then():
                ChunkSize.assign(self.constant(ChunkSize.type, 1))
                num_thread.assign(N.cast(num_thread.type))

        # Populate workqueue for all threads
        self._populate_workqueues(workqueues, N, ChunkSize, num_thread)

        # Populate contexts for all threads
        self._populate_context(contexts, common, num_thread)

        # Dispatch worker threads
        self._dispatch_worker(worker, contexts,  num_thread)

        ## DEBUG ONLY ##
        # Check for race condition
        if True:
            total_completed = self.var(C.intp, 0, name='total_completed')
            for t in range(ThreadCount):
                cur_ctxt = contexts[t].as_struct(Context)
                total_completed += cur_ctxt.completed
                # self.debug(cur_ctxt.id, 'completed', cur_ctxt.completed)

            with self.ifelse( total_completed == N ) as ifelse:
                with ifelse.then():
                    # self.debug("All is well!")
                    pass # keep quite if all is well
                with ifelse.otherwise():
                    self.debug("ERROR: race occurred! Trigger segfault")
                    self.unreachable()

        # Return
        self.ret()

    def _populate_workqueues(self, workqueues, N, ChunkSize, num_thread):
        '''loop over all threads and populate the workqueue for each of them.
        '''
        ONE = self.constant(num_thread.type, 1)
        with self.for_range(num_thread) as (loop, i):
            cur_wq = workqueues[i].as_struct(WorkQueue)
            cur_wq.next.assign(i.cast(ChunkSize.type) * ChunkSize)
            cur_wq.last.assign((i + ONE).cast(ChunkSize.type) * ChunkSize)
            cur_wq.lock.assign(self.constant(C.int, 0))
        # end loop
        last_wq = workqueues[num_thread - ONE].as_struct(WorkQueue)
        last_wq.last.assign(N)

    def _populate_context(self, contexts, common, num_thread):
        '''loop over all threads and populate contexts for each of them.
        '''
        ONE = self.constant(num_thread.type, 1)
        with self.for_range(num_thread) as (loop, i):
            cur_ctxt = contexts[i].as_struct(Context)
            cur_ctxt.common.assign(common.reference())
            cur_ctxt.id.assign(i)
            cur_ctxt.completed.assign(
                                    self.constant_null(cur_ctxt.completed.type))

class ParallelUFuncPosixMixin(object):
    '''ParallelUFunc mixin that implements _dispatch_worker to use pthread.
    '''
    def _dispatch_worker(self, worker, contexts, num_thread):
        api = PThreadAPI(self)
        NULL = self.constant_null(C.void_p)

        threads = self.array(api.pthread_t, num_thread, name='threads')

        # self.debug("launch threads")
        # TODO error handling

        ONE = self.constant(num_thread.type, 1)
        with self.for_range(num_thread) as (loop, i):
            api.pthread_create(threads[i].reference(), NULL, worker,
                               contexts[i].reference().cast(C.void_p))

        with self.for_range(num_thread) as (loop, i):
            api.pthread_join(threads[i], NULL)

class UFuncCore(CDefinition):
    '''core work of a ufunc worker thread

    Subclass to implement UFuncCore._do_work

    Generates the workqueue handling and work stealing and invoke
    the work function for each work item.
    '''
    _name_ = 'ufunc_worker'
    _argtys_ = [
        ('context', C.pointer(Context.llvm_type())),
        ]

    def body(self, context):
        context = context.as_struct(Context)
        common = context.common.as_struct(ContextCommon)
        tid = context.id

        # self.debug("start thread", tid, "/", common.num_thread)
        workqueue = common.workqueues[tid].as_struct(WorkQueue)

        self._do_workqueue(common, workqueue, tid, context.completed)
        self._do_work_stealing(common, tid, context.completed) # optional

        self.ret()

    def _do_workqueue(self, common, workqueue, tid, completed):
        '''process local workqueue.
        '''
        ZERO = self.constant_null(C.int)

        with self.forever() as loop:
            workqueue.Lock()
            # Critical section
            item = self.var_copy(workqueue.next, name='item')
            workqueue.next += self.constant(item.type, 1)
            last = self.var_copy(workqueue.last, name='last')
            # Release
            workqueue.Unlock()

            with self.ifelse( item >= last ) as ifelse:
                with ifelse.then():
                    loop.break_loop()

            self._do_work(common, item, tid)
            completed += self.constant(completed.type, 1)

    def _do_work_stealing(self, common, tid, completed):
        '''steal work from other workqueues.
        '''
        # self.debug("start work stealing", tid)
        steal_continue = self.var(C.int, 1)
        STEAL_STOP = self.constant_null(steal_continue.type)

        # Loop until all workqueues are done.
        with self.loop() as loop:
            with loop.condition() as setcond:
                setcond( steal_continue != STEAL_STOP )

            with loop.body():
                steal_continue.assign(STEAL_STOP)
                self._do_work_stealing_innerloop(common, steal_continue, tid,
                                                 completed)

    def _do_work_stealing_innerloop(self, common, steal_continue, tid,
                                    completed):
        '''loop over all other threads and try to steal work.
        '''
        with self.for_range(common.num_thread) as (loop, i):
            with self.ifelse( i != tid ) as ifelse:
                with ifelse.then():
                    otherqueue = common.workqueues[i].as_struct(WorkQueue)
                    self._do_work_stealing_check(common, otherqueue,
                                                 steal_continue, tid,
                                                 completed)

    def _do_work_stealing_check(self, common, otherqueue, steal_continue, tid,
                                completed):
        '''check the workqueue for any remaining work and steal it.
        '''
        otherqueue.Lock()
        # Acquired
        ONE = self.constant(otherqueue.last.type, 1)
        STEAL_CONTINUE = self.constant(steal_continue.type, 1)
        with self.ifelse(otherqueue.next < otherqueue.last) as ifelse:
            with ifelse.then():
                otherqueue.last -= ONE
                item = self.var_copy(otherqueue.last)

                otherqueue.Unlock()
                # Released

                self._do_work(common, item, tid)
                completed += self.constant(completed.type, 1)

                # Mark incomplete thread
                steal_continue.assign(STEAL_CONTINUE)

            with ifelse.otherwise():
                otherqueue.Unlock()
                # Released

    def _do_work(self, common, item, tid):
        '''prepare to call the actual work function

        Implementation depends on number and type of arguments.
        '''
        raise NotImplementedError

class SpecializedParallelUFunc(CDefinition):
    '''a generic ufunc that wraps ParallelUFunc, UFuncCore and the workload
    '''
    _argtys_ = [
        ('args',       C.pointer(C.char_p)),
        ('dimensions', C.pointer(C.intp)),
        ('steps',      C.pointer(C.intp)),
        ('data',       C.void_p),
    ]

    def body(self, args, dimensions, steps, data,):
        pufunc = self.depends(self.PUFuncDef)
        core = self.depends(self.CoreDef)
        func = self.depends(self.FuncDef)
        to_void_p = lambda x: x.cast(C.void_p)
        pufunc(to_void_p(func), to_void_p(core), args, dimensions, steps, data)
        self.ret()

    @classmethod
    def specialize(cls, pufunc_def, core_def, func_def):
        '''specialize to a combination of ParallelUFunc, UFuncCore and workload
        '''
        cls._name_ = 'specialized_%s_%s_%s'% (pufunc_def, core_def, func_def)
        cls.PUFuncDef = pufunc_def
        cls.CoreDef = core_def
        cls.FuncDef = func_def

class PThreadAPI(CExternal):
    '''external declaration of pthread API
    '''
    pthread_t = C.void_p

    pthread_create = Type.function(C.int,
                                   [C.pointer(pthread_t),  # thread_t
                                    C.void_p,              # thread attr
                                    C.void_p,              # function
                                    C.void_p])             # arg

    pthread_join = Type.function(C.int, [C.void_p, C.void_p])


class UFuncCoreGeneric(UFuncCore):
    '''A generic ufunc core worker from LLVM function type
    '''
    def _do_work(self, common, item, tid):
        ufunc_type = Type.function(self.RETTY, self.ARGTYS)
        ufunc_ptr = CFunc(self, common.func.cast(C.pointer(ufunc_type)).value)

        get_offset = lambda B, S, T: B[item * S].reference().cast(C.pointer(T))

        indata = []
        for i, argty in enumerate(self.ARGTYS):
            ptr = get_offset(common.args[i], common.steps[i], argty)
            indata.append(ptr.load())

        out_index = len(self.ARGTYS)
        outptr = get_offset(common.args[out_index], common.steps[out_index],
                         self.RETTY)

        res = ufunc_ptr(*indata)
        outptr.store(res)

    @classmethod
    def specialize(cls, fntype):
        '''specialize to a LLVM function type

        fntype : a LLVM function type (llvm.core.FunctionType)
        '''
        cls._name_ = '.'.join([cls._name_] +
                              map(str, [fntype.return_type] + fntype.args))

        cls.RETTY = fntype.return_type
        cls.ARGTYS = tuple(fntype.args)


if sys.platform not in ['win32']:
    class ParallelUFuncPlatform(ParallelUFunc, ParallelUFuncPosixMixin):
        pass
else:
    raise NotImplementedError("Threading for %s" % sys.platform)

_llvm_ty_str_to_numpy = {
            'i8'     : np.int8,
            'i16'    : np.int16,
            'i32'    : np.int32,
            'i64'    : np.int64,
            'float'  : np.float32,
            'double' : np.float64,
        }

def _llvm_ty_to_numpy(ty):
    return _llvm_ty_str_to_numpy[str(ty)]

def parallel_vectorize_from_func(lfunclist, engine=None):
    '''create ufunc from a llvm.core.Function

    lfunclist : a single or iterable of llvm.core.Function instance
    engine : [optional] a llvm.ee.ExecutionEngine instance

    If engine is given, return a function object which can be called
    from python.
    Otherwise, return the specialized ufunc(s) as a llvm.core.Function(s).
    '''
    import multiprocessing
    NUM_CPU = multiprocessing.cpu_count()

    try:
        iter(lfunclist)
    except TypeError:
        lfunclist = [lfunclist]

    spuflist = []
    for lfunc in lfunclist:
        def_spuf = SpecializedParallelUFunc(
                                    ParallelUFuncPlatform(num_thread=NUM_CPU),
                                    UFuncCoreGeneric(lfunc.type.pointee),
                                    CFuncRef(lfunc))
        spuf = def_spuf(lfunc.module)
        spuflist.append(spuf)

    if engine is None:
        # No engine given, just return the llvm definitions
        if len(spuflist)==1:
            return spuflist[0]
        else:
            return spuflist

    # We have an engine, build ufunc
    from numbapro._internal import fromfunc

    try:
        ptr_t = long
    except:
        ptr_t = int
        assert False, "Have not check this yet" # Py3.0?

    ptrlist = []
    tyslist = []
    datlist = []
    for i, spuf in enumerate(spuflist):
        fntype = lfunclist[i].type.pointee
        fptr = engine.get_pointer_to_function(spuf)
        argct = len(fntype.args)
        if i == 0: # for the first
            inct = argct
            outct = 1
        elif argct != inct:
            raise TypeError("All functions must have equal number of arguments")

        get_typenum = lambda T:np.dtype(_llvm_ty_to_numpy(T)).num
        assert fntype.return_type != C.void
        tys = list(map(get_typenum, list(fntype.args) + [fntype.return_type]))

        ptrlist.append(ptr_t(fptr))
        tyslist.append(tys)
        datlist.append(None)

    # Becareful that fromfunc does not provide full error checking yet.
    # If typenum is out-of-bound, we have nasty memory corruptions.
    # For instance, -1 for typenum will cause segfault.
    # If elements of type-list (2nd arg) is tuple instead,
    # there will also memory corruption. (Seems like code rewrite.)
    ufunc = fromfunc(ptrlist, tyslist, inct, outct, datlist)
    return ufunc

from numbapro.translate import Translate

class ParallelVectorize(object):
    def __init__(self, func):
        self.pyfunc = func
        self.translates = []

    def add(self, *args, **kwargs):
        t = Translate(self.pyfunc, *args, **kwargs)
        t.translate()
        self.translates.append(t)

    def build_ufunc(self):
        assert self.translates, "No translation"
        lfunclist = self._get_lfunc_list()
        engine = self.translates[0]._get_ee()
        return parallel_vectorize_from_func(lfunclist, engine=engine)

    def _get_lfunc_list(self):
        return [t.lfunc for t in self.translates]

class _CudaStagingCaller(CDefinition):
    def body(self, *args, **kwargs):
        worker = self.depends(self.WorkerDef)
        inputs = args[:-2]
        output, ct = args[-2:]

        # get current thread index
        tid_x = self.get_intrinsic(INTR_PTX_READ_TID_X, [])
        ntid_x = self.get_intrinsic(INTR_PTX_READ_NTID_X, [])
        ctaid_x = self.get_intrinsic(INTR_PTX_READ_CTAID_X, [])

        tid = self.var_copy(tid_x())
        blkdim = self.var_copy(ntid_x())
        blkid = self.var_copy(ctaid_x())

        i = tid + blkdim * blkid
        with self.ifelse( i >= ct ) as ifelse: # stop condition
            with ifelse.then():
                self.ret()

        res = worker(*map(lambda x: x[i], inputs))
        res.value.calling_convention = CC_PTX_DEVICE
        output[i].assign(res)

        self.ret()

    @classmethod
    def specialize(cls, worker, fntype):
        cls._name_ = ("_cukernel_%s" % worker).replace('.', '_2E_')

        args = cls._argtys_ = []
        inargs = cls.InArgs = []

        # input arguments
        for i, ty in enumerate(fntype.args):
            cur = ('in%d' % i, cls._pointer(ty))
            args.append(cur)
            inargs.append(cur)

        # output arguments
        cur = ('out', cls._pointer(fntype.return_type))
        args.append(cur)
        cls.OutArg = cur

        # extra arguments
        args.append(('ct', C.int))

        cls.WorkerDef = worker

    @classmethod
    def _pointer(cls, ty):
        return C.pointer(ty)

class CudaVectorize(ParallelVectorize):
    def __init__(self, func):
        super(CudaVectorize, self).__init__(func)
        self.module = Module.new("ptx_%s" % func.func_name)

    def add(self, *args, **kwargs):
        kwargs.update({'module': self.module})
        t = Translate(self.pyfunc, *args, **kwargs)
        t.translate()
        self.translates.append(t)

    def build_ufunc(self):
        # quick & dirty tryout
        # PyCuda should be optional
        from pycuda import driver as cudriver
        from pycuda.autoinit import device, context # use default
        from math import ceil

        lfunclist = self._get_lfunc_list()

        # setup optimizer for the staging caller
        fpm = FunctionPassManager.new(self.module)
        pmbldr = PassManagerBuilder.new()
        pmbldr.opt_level = 3
        pmbldr.populate(fpm)


        functype_list = []
        for lfunc in lfunclist: # generate caller for all function
            lfty = lfunc.type.pointee
            nptys = tuple(map(np.dtype, map(_llvm_ty_to_numpy, lfty.args)))

            lcaller = self._build_caller(lfunc)
            fpm.run(lcaller)    # run the optimizer

            # unicode problem?
            fname = lcaller.name
            if type(fname) is unicode:
                fname = fname.encode('utf-8')
            functype_list.append([fname,
                                 _llvm_ty_to_numpy(lfty.return_type),
                                 nptys])

        # force inlining & trim internal function
        pm = PassManager.new()
        pm.add(PASS_INLINE)
        pm.run(self.module)

        # generate ptx asm
        cc = 'compute_%d%d' % device.compute_capability() # select device cc
        if HAS_PTX:
            arch = 'ptx%d' % C.intp.width # select by host pointer size
        elif HAS_NVPTX:
            arch = {32: 'nvptx', 64: 'nvptx64'}[C.intp.width]
        else:
            raise Exception("llvmpy does not have PTX/NVPTX support")
        assert C.intp.width in [32, 64]
        ptxtm = TargetMachine.lookup(arch, cpu=cc, opt=3) # TODO: ptx64 option
        ptxasm = ptxtm.emit_assembly(self.module)
        print(ptxasm)

        # prepare device
        ptxmodule = cudriver.module_from_buffer(ptxasm)

        devattr = device.get_attributes()
        MAX_THREAD = devattr[cudriver.device_attribute.MAX_THREADS_PER_BLOCK]
        MAX_BLOCK = devattr[cudriver.device_attribute.MAX_BLOCK_DIM_X]


        # get function
        kernel_list = [(ptxmodule.get_function(name), retty, argty)
                         for name, retty, argty in functype_list]

        def _ufunc_hack(*args):
            # determine type & kernel
            # FIXME: this is just a hack currently
            tys = tuple(map(lambda x: x.dtype, args))
            for kernel, retty, argtys in kernel_list:
                if argtys == tys:
                    break

            # prepare broadcasted arrays
            bcargs = np.broadcast_arrays(*args)
            N = bcargs[0].shape[0]

            retary = np.empty(N, dtype=retty)

            # device compute
            if N > MAX_THREAD:
                threadct =  MAX_THREAD, 1, 1
                blockct = int(ceil(float(N) / MAX_THREAD)), 1
            else:
                threadct =  N, 1, 1
                blockct  =  1, 1

            kernelargs = list(map(cudriver.In, bcargs))
            kernelargs += [cudriver.Out(retary), np.int32(N)]

            time = kernel(*kernelargs,
                          block=threadct, grid=blockct,
                          time_kernel=True)
            print 'kernel time = %s' % time

            return retary

        return _ufunc_hack

    def _build_caller(self, lfunc):
        lfunc.calling_convention = CC_PTX_DEVICE
        lfunc.linkage = LINKAGE_INTERNAL       # do not emit device function
        lcaller_def = _CudaStagingCaller(CFuncRef(lfunc), lfunc.type.pointee)
        lcaller = lcaller_def(self.module)
        lcaller.calling_convention = CC_PTX_KERNEL
        return lcaller




