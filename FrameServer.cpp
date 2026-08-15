// ============================================================================
// FrameServer.cpp — Implementation of the shared memory reader
// ============================================================================
#include "FrameServer.h"
#include "Logger.h"
#include <mferror.h>

// 32-byte header + NV12 frame size (1920x1080)
static const DWORD TOTAL_MEM_SIZE = sizeof(SharedMemHeader) - 1 + 1920 * 1080 * 3 / 2;

FrameServer::FrameServer()
    : _hMapFile(nullptr)
    , _hMutex(nullptr)
    , _header(nullptr)
    , _lastIndex(0)
{
}

FrameServer::~FrameServer()
{
    if (_header)
        UnmapViewOfFile(_header);
    if (_hMapFile)
        CloseHandle(_hMapFile);
    if (_hMutex)
        CloseHandle(_hMutex);
}

HRESULT FrameServer::Initialize()
{
    // Create a security descriptor with a NULL DACL to allow full access.
    // This is necessary because this DLL runs in Session 0 (LOCAL SERVICE), 
    // and the companion script runs in the interactive user session.
    SECURITY_DESCRIPTOR sd;
    if (!InitializeSecurityDescriptor(&sd, SECURITY_DESCRIPTOR_REVISION))
        return HRESULT_FROM_WIN32(GetLastError());

    if (!SetSecurityDescriptorDacl(&sd, TRUE, NULL, FALSE))
        return HRESULT_FROM_WIN32(GetLastError());

    SECURITY_ATTRIBUTES sa;
    sa.nLength              = sizeof(sa);
    sa.lpSecurityDescriptor = &sd;
    sa.bInheritHandle       = FALSE;

    // Create the global file mapping object. If a mapping with this name
    // already exists (a re-activated source, or the host creating the camera
    // while the service mapping is still alive), CreateFileMappingW reuses it
    // and the data written by the companion must NOT be wiped.
    _hMapFile = CreateFileMappingW(
        INVALID_HANDLE_VALUE,   // Backed by the system paging file
        &sa,
        PAGE_READWRITE,
        0,
        TOTAL_MEM_SIZE,
        SHARED_MEM_NAME         // L"Global\\BestCam_SharedMem"
    );

    if (!_hMapFile)
        return HRESULT_FROM_WIN32(GetLastError());

    // Capture immediately: a later successful API call may clear the error
    const DWORD dwMapErr = GetLastError();

    // Map the view into our process address space
    _header = (SharedMemHeader*)MapViewOfFile(
        _hMapFile, 
        FILE_MAP_READ | FILE_MAP_WRITE, 
        0, 
        0, 
        0
    );

    if (!_header)
        return HRESULT_FROM_WIN32(GetLastError());

    if (dwMapErr == ERROR_ALREADY_EXISTS)
    {
        // Existing mapping (another source instance or the host owns it):
        // keep whatever the companion has written; do not zero it out.
        // Also open the existing cross-process mutex: without it the reads
        // are lockless and can tear the header while the companion switches
        // resolutions (new w/h against the old frameSize -> garbled/black
        // frames for the client).
        _hMutex = OpenMutexW(SYNCHRONIZE, FALSE, MUTEX_NAME);
        Log("FrameServer: reusing existing mapping");
        return S_OK;
    }

    // We created the mapping: zero-initialize and set up default metadata so
    // the companion script knows the dimensions we expect. desired stays 0
    // until a client negotiates a media type (SetDesiredResolution).
    ZeroMemory(_header, sizeof(SharedMemHeader));
    _header->width     = 1920;
    _header->height    = 1080;
    _header->stride    = 1920;
    _header->frameSize = 1920 * 1080 * 3 / 2;
    Log("FrameServer: created new mapping %u bytes", TOTAL_MEM_SIZE);

    // Create the cross-process mutex (non-fatal if this fails, we can run lockless)
    _hMutex = CreateMutexW(&sa, FALSE, MUTEX_NAME);

    return S_OK;
}

HRESULT FrameServer::GetLatestFrame(BYTE** data, DWORD* length, UINT64* frameIndex)
{
    if (!_header)
        return E_FAIL;

    if (_hMutex)
        WaitForSingleObject(_hMutex, 5);

    // If the frame index hasn't changed, there is no new frame
    if (_header->frameIndex == _lastIndex)
    {
        if (_hMutex) ReleaseMutex(_hMutex);
        return E_PENDING;
    }

    // Pass back pointers directly into the shared memory segment
    *data       = _header->data;
    *length     = _header->frameSize;
    *frameIndex = _header->frameIndex;
    _lastIndex  = _header->frameIndex;

    if (_hMutex) ReleaseMutex(_hMutex);
    return S_OK;
}

// Copy the latest frame into a caller-owned buffer while holding the mutex,
// so the companion's writer (which also holds the mutex) cannot tear the
// frame mid-copy. This is what the stream uses for its per-frame cache.
HRESULT FrameServer::CopyLatestFrame(BYTE* dst, DWORD capacity, DWORD* length, UINT64* frameIndex)
{
    if (!_header || !dst || !length)
        return E_FAIL;

    if (_hMutex)
        WaitForSingleObject(_hMutex, 5);

    if (_header->frameIndex == _lastIndex)
    {
        if (_hMutex) ReleaseMutex(_hMutex);
        return E_PENDING;
    }

    if (capacity < _header->frameSize)
    {
        if (_hMutex) ReleaseMutex(_hMutex);
        return MF_E_BUFFERTOOSMALL;
    }

    memcpy(dst, _header->data, _header->frameSize);
    *length     = _header->frameSize;
    *frameIndex = _header->frameIndex;
    _lastIndex  = _header->frameIndex;

    if (_hMutex) ReleaseMutex(_hMutex);
    return S_OK;
}

UINT32 FrameServer::GetWidth()  const { return _header ? _header->width  : 0; }
UINT32 FrameServer::GetHeight() const { return _header ? _header->height : 0; }

void FrameServer::SetDesiredResolution(UINT32 width, UINT32 height)
{
    if (!_header || width == 0 || height == 0)
        return;

    if (_hMutex)
        WaitForSingleObject(_hMutex, 5);
    _header->desiredWidth  = width;
    _header->desiredHeight = height;
    if (_hMutex)
        ReleaseMutex(_hMutex);
}
