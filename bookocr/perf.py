# -*- coding: utf-8 -*-
"""性能自检：按本机 CPU / 内存 / 供电状态自动选择 OCR 并行度。

目标：用这台机器大约 80% 的算力跑 OCR，留 20% 给用户干别的事；
笔记本用电池时自动减半，内存不够时自动减进程。
"""
import ctypes
import os


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [('dwLength', ctypes.c_ulong),
                ('dwMemoryLoad', ctypes.c_ulong),
                ('ullTotalPhys', ctypes.c_ulonglong),
                ('ullAvailPhys', ctypes.c_ulonglong),
                ('ullTotalPageFile', ctypes.c_ulonglong),
                ('ullAvailPageFile', ctypes.c_ulonglong),
                ('ullTotalVirtual', ctypes.c_ulonglong),
                ('ullAvailVirtual', ctypes.c_ulonglong),
                ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]


class _POWERSTATUS(ctypes.Structure):
    _fields_ = [('ACLineStatus', ctypes.c_ubyte),
                ('BatteryFlag', ctypes.c_ubyte),
                ('BatteryLifePercent', ctypes.c_ubyte),
                ('Reserved1', ctypes.c_ubyte),
                ('BatteryLifeTime', ctypes.c_ulong),
                ('BatteryFullLifeTime', ctypes.c_ulong)]


class _FILETIME(ctypes.Structure):
    _fields_ = [('dwLowDateTime', ctypes.c_ulong),
                ('dwHighDateTime', ctypes.c_ulong)]


def avail_mem_gb():
    try:
        ms = _MEMORYSTATUSEX()
        ms.dwLength = ctypes.sizeof(ms)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
            return ms.ullAvailPhys / 1024.0 ** 3
    except Exception:
        pass
    return None


def on_ac_power():
    """True=插电，False=电池，None=未知。"""
    try:
        ps = _POWERSTATUS()
        if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(ps)):
            if ps.ACLineStatus == 1:
                return True
            if ps.ACLineStatus == 0:
                return False
    except Exception:
        pass
    return None


def cpu_load(interval=0.3):
    """粗略 CPU 占用率 0..1（含其他程序）。"""
    try:
        idle1, kern1, user1 = _FILETIME(), _FILETIME(), _FILETIME()
        ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle1),
                                              ctypes.byref(kern1),
                                              ctypes.byref(user1))

        def val(ft):
            return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

        import time
        i1, k1, u1 = val(idle1), val(kern1), val(user1)
        time.sleep(interval)
        idle2, kern2, user2 = _FILETIME(), _FILETIME(), _FILETIME()
        ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle2),
                                              ctypes.byref(kern2),
                                              ctypes.byref(user2))
        i2, k2, u2 = val(idle2), val(kern2), val(user2)
        idle = max(1, i2 - i1)
        total = max(1, (k2 - k1) + (u2 - u1))
        return max(0.0, min(1.0, 1.0 - idle / float(total)))
    except Exception:
        return None


def auto_workers(threads_per_worker=3, share=0.8):
    """返回 (workers, info)。workers 个进程，每个 3 线程。"""
    logical = os.cpu_count() or 4
    threads = max(2, int(round(logical * share)))
    workers = max(1, threads // threads_per_worker)

    ram = avail_mem_gb()
    if ram is not None:
        # 每个 OCR 进程实测约 0.5-0.8GB；预留 1GB 给系统和用户
        workers = min(workers, max(1, int((ram - 1.0) / 0.8)))

    ac = on_ac_power()
    if ac is False:
        workers = max(1, workers // 2)

    load = cpu_load()
    if load is not None and load > 0.35:
        workers = max(1, int(workers * max(0.4, 1.0 - load)))

    workers = min(workers, 12)
    info = {'logical': logical, 'threads': workers * threads_per_worker,
            'ram': ram, 'ac': ac, 'load': load,
            'pct': int(round(workers * threads_per_worker * 100.0 / logical))}
    return workers, info


def describe(info):
    bits = [u'%d 逻辑核' % info['logical']]
    if info.get('ram') is not None:
        bits.append(u'可用内存 %.1fGB' % info['ram'])
    if info.get('ac') is False:
        bits.append(u'电池供电，已减半')
    if info.get('load') is not None and info['load'] > 0.35:
        bits.append(u'当前已占用 %d%%，已让路' % int(info['load'] * 100))
    bits.append(u'实际占用约 %d%% 算力' % info['pct'])
    return u'，'.join(bits)
