// ============================================================================
// MediaStream.cpp — Implementation of the video stream
// ============================================================================
#include "MediaStream.h"
#include "MediaSource.h"
#include <mfapi.h>
#include <mferror.h>

#pragma comment(lib, "mfplat.lib")
#pragma comment(lib, "mf.lib")

VirtualCamMediaStream::VirtualCamMediaStream() : _active(false), _lastFrameIndex(0) {}
VirtualCamMediaStream::~VirtualCamMediaStream() {}

// Supported resolutions (NV12, 30fps). Clients (e.g. ExVR) negotiate one of
// these; frame data size follows the shared-memory header written by the
// companion script, which must match the negotiated resolution.
static const struct ResEntry { UINT32 width, height; } kSupportedResolutions[] = {
    {1920, 1080},
    {1280, 720},
    {800, 600},
    {800, 450},
    {640, 480},
};

// Cache buffer covers the largest frame the mapping can hold.
static const DWORD MAX_FRAME_BYTES = 1920 * 1080 * 3 / 2;

HRESULT VirtualCamMediaStream::RuntimeClassInitialize(VirtualCamMediaSource* pSource)
{
    _source = pSource;

    HRESULT hr = MFCreateEventQueue(&_eventQueue);
    if (FAILED(hr)) return hr;

    std::vector<Microsoft::WRL::ComPtr<IMFMediaType>> types;
    types.reserve(std::size(kSupportedResolutions));
    for (const auto& res : kSupportedResolutions)
    {
        Microsoft::WRL::ComPtr<IMFMediaType> mediaType;
        hr = CreateMediaType(res.width, res.height, &mediaType);
        if (FAILED(hr)) return hr;
        types.push_back(std::move(mediaType));
    }

    std::vector<IMFMediaType*> typeArr;
    typeArr.reserve(types.size());
    for (const auto& mt : types)
        typeArr.push_back(mt.Get());

    hr = MFCreateStreamDescriptor(0, (DWORD)typeArr.size(), typeArr.data(), &_streamDescriptor);
    if (FAILED(hr)) return hr;

    // Must be set to prevent MF_E_ATTRIBUTENOTFOUND
    hr = SetCurrentMediaTypeOnHandler();
    if (FAILED(hr)) return hr;

    // Initialize the shared memory frame reader
    _frameServer = std::make_unique<FrameServer>();
    _frameServer->Initialize(); // Non-fatal if the companion script isn't running yet

    return S_OK;
}

HRESULT VirtualCamMediaStream::CreateMediaType(UINT32 width, UINT32 height, IMFMediaType** ppMediaType)
{
    const UINT32 STRIDE = width;
    // NV12 format size: Y plane + (UV plane)
    const UINT32 SAMPLE_SIZE = width * height * 3 / 2;

    Microsoft::WRL::ComPtr<IMFMediaType> mediaType;
    HRESULT hr = MFCreateMediaType(&mediaType);
    if (FAILED(hr)) return hr;

    hr = mediaType->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
    if (FAILED(hr)) return hr;
    hr = mediaType->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_NV12);
    if (FAILED(hr)) return hr;
    hr = MFSetAttributeSize(mediaType.Get(), MF_MT_FRAME_SIZE, width, height);
    if (FAILED(hr)) return hr;
    hr = MFSetAttributeRatio(mediaType.Get(), MF_MT_FRAME_RATE, 30, 1);
    if (FAILED(hr)) return hr;
    hr = MFSetAttributeRatio(mediaType.Get(), MF_MT_PIXEL_ASPECT_RATIO, 1, 1);
    if (FAILED(hr)) return hr;
    
    hr = mediaType->SetUINT32(MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive);
    if (FAILED(hr)) return hr;
    hr = mediaType->SetUINT32(MF_MT_ALL_SAMPLES_INDEPENDENT, TRUE);
    if (FAILED(hr)) return hr;
    hr = mediaType->SetUINT32(MF_MT_FIXED_SIZE_SAMPLES, TRUE);
    if (FAILED(hr)) return hr;
    hr = mediaType->SetUINT32(MF_MT_SAMPLE_SIZE, SAMPLE_SIZE);
    if (FAILED(hr)) return hr;
    hr = mediaType->SetUINT32(MF_MT_DEFAULT_STRIDE, STRIDE);
    if (FAILED(hr)) return hr;
    hr = mediaType->SetUINT32(MF_MT_VIDEO_NOMINAL_RANGE, MFNominalRange_Normal);
    if (FAILED(hr)) return hr;
    hr = mediaType->SetUINT32(MF_MT_VIDEO_PRIMARIES, MFVideoPrimaries_BT709);
    if (FAILED(hr)) return hr;
    hr = mediaType->SetUINT32(MF_MT_YUV_MATRIX, MFVideoTransferMatrix_BT709);
    if (FAILED(hr)) return hr;
    hr = mediaType->SetUINT32(MF_MT_TRANSFER_FUNCTION, MFVideoTransFunc_709);
    if (FAILED(hr)) return hr;

    *ppMediaType = mediaType.Detach();
    return S_OK;
}

HRESULT VirtualCamMediaStream::SetCurrentMediaTypeOnHandler()
{
    Microsoft::WRL::ComPtr<IMFMediaTypeHandler> handler;
    HRESULT hr = _streamDescriptor->GetMediaTypeHandler(&handler);
    if (FAILED(hr)) return hr;

    Microsoft::WRL::ComPtr<IMFMediaType> mediaType;
    hr = handler->GetMediaTypeByIndex(0, &mediaType);
    if (FAILED(hr)) return hr;

    return handler->SetCurrentMediaType(mediaType.Get());
}

HRESULT VirtualCamMediaStream::GetNegotiatedType(IMFMediaType** ppType)
{
    if (!ppType) return E_POINTER;
    Microsoft::WRL::ComPtr<IMFMediaTypeHandler> handler;
    HRESULT hr = _streamDescriptor->GetMediaTypeHandler(&handler);
    if (FAILED(hr)) return hr;
    return handler->GetCurrentMediaType(ppType);
}

HRESULT VirtualCamMediaStream::SetNegotiatedType(IMFMediaType* pType)
{
    if (!pType) return E_POINTER;
    Microsoft::WRL::ComPtr<IMFMediaTypeHandler> handler;
    HRESULT hr = _streamDescriptor->GetMediaTypeHandler(&handler);
    if (FAILED(hr)) return hr;
    return handler->SetCurrentMediaType(pType);
}

void VirtualCamMediaStream::SetStreamDescriptor(IMFStreamDescriptor* pDescriptor)
{
    if (pDescriptor)
    {
        _streamDescriptor = pDescriptor;
        _mediaTypeHandler = nullptr;  // the descriptor may have been replaced: drop the cached handler
    }
}

void VirtualCamMediaStream::SetActive(bool active)
{
    _active = active;
}

// Track the currently negotiated media type so RequestSample can compare it
// against the frame size in shared memory (a mismatch means the companion is
// mid-switch and black frames are delivered instead of garbled ones). Called
// on every RequestSample: the negotiated type can change between client
// open() calls (and even per-open).
void VirtualCamMediaStream::SyncDesiredResolution()
{
    if (!_frameServer)
        return;

    // The stream descriptor never changes for the lifetime of the stream;
    // caching the handler avoids a COM AddRef/Release + object creation on
    // every RequestSample (OBS can pull at its own pace, so this is the
    // hottest per-frame path in the DLL).
    if (!_mediaTypeHandler)
    {
        Microsoft::WRL::ComPtr<IMFMediaTypeHandler> handler;
        if (FAILED(_streamDescriptor->GetMediaTypeHandler(&handler)))
            return;
        _mediaTypeHandler = handler;
    }

    Microsoft::WRL::ComPtr<IMFMediaType> mediaType;
    if (FAILED(_mediaTypeHandler->GetCurrentMediaType(&mediaType)))
        return;

    UINT32 width = 0, height = 0;
    if (FAILED(MFGetAttributeSize(mediaType.Get(), MF_MT_FRAME_SIZE, &width, &height)))
        return;

    _curW = width;
    _curH = height;
}

HRESULT VirtualCamMediaStream::FireStreamStarted(const PROPVARIANT* pvarStartPosition)
{
    // Important: MEStreamStarted must be fired AFTER the source fires MENewStream
    return _eventQueue->QueueEventParamVar(MEStreamStarted, GUID_NULL, S_OK, pvarStartPosition);
}

STDMETHODIMP VirtualCamMediaStream::GetMediaSource(IMFMediaSource** ppMediaSource)
{
    return _source.CopyTo(ppMediaSource);
}

STDMETHODIMP VirtualCamMediaStream::GetStreamDescriptor(IMFStreamDescriptor** ppStreamDescriptor)
{
    if (!ppStreamDescriptor) return E_POINTER;
    *ppStreamDescriptor = _streamDescriptor.Get();
    if (_streamDescriptor) _streamDescriptor->AddRef();
    return S_OK;
}

STDMETHODIMP VirtualCamMediaStream::RequestSample(IUnknown* pToken)
{
    if (!_active)
        return MF_E_MEDIA_SOURCE_WRONGSTATE;

    SyncDesiredResolution();

    BYTE*  srcData   = nullptr;
    DWORD  srcLength = 0;
    UINT64 frameIdx  = 0;

    // Preallocate the max-size cache once; CopyLatestFrame overwrites it
    // while holding the cross-process mutex, so the companion's writer can
    // never tear a frame mid-copy.
    if (_lastFrame.size() != MAX_FRAME_BYTES)
        _lastFrame.resize(MAX_FRAME_BYTES);

    HRESULT hr = _frameServer->CopyLatestFrame(_lastFrame.data(), (DWORD)_lastFrame.size(), &srcLength, &frameIdx);

    if (SUCCEEDED(hr) && srcLength > 0)
    {
        // The companion may still be mid-switch when the client negotiates a
        // different resolution: the frame in shared memory then has a size
        // that does not match the negotiated media type, and delivering it
        // would render a garbled/colored frame. Deliver a black frame instead
        // and wait for the next matching one — the client keeps running and
        // gets real frames as soon as the companion matches the resolution.
        if (_frameServer->GetWidth() != _curW || _frameServer->GetHeight() != _curH)
        {
            QueueBlankSample(pToken);
            return S_OK;
        }

        _lastFrameLen = srcLength;
        _lastFrameIndex = frameIdx;
    }
    else if (FAILED(hr) && hr != E_PENDING)
    {
        _lastFrameLen = 0;
    }

    const BYTE* frameData = nullptr;
    DWORD       frameLen  = 0;

    if (_lastFrameLen > 0)
    {
        frameData = _lastFrame.data();
        frameLen  = _lastFrameLen;
    }
    else
    {
        // Provide a blank frame to prevent green glitching while waiting for first frame
        QueueBlankSample(pToken);
        return S_OK;
    }

    Microsoft::WRL::ComPtr<IMFSample> sample;
    hr = MFCreateSample(&sample);
    if (FAILED(hr)) return hr;

    Microsoft::WRL::ComPtr<IMFMediaBuffer> buffer;
    hr = MFCreateMemoryBuffer(frameLen, &buffer);
    if (FAILED(hr)) return hr;

    BYTE* dst = nullptr;
    hr = buffer->Lock(&dst, nullptr, nullptr);
    if (FAILED(hr)) return hr;
    
    // Copy NV12 frame to the MF buffer
    memcpy(dst, frameData, frameLen);
    
    buffer->Unlock();
    buffer->SetCurrentLength(frameLen);

    sample->AddBuffer(buffer.Get());
    sample->SetSampleTime(MFGetSystemTime());
    sample->SetSampleDuration(333333); // ~30 fps in 100-nanosecond units

    if (pToken)
        sample->SetUnknown(MFSampleExtension_Token, pToken);

    _eventQueue->QueueEventParamUnk(MEMediaSample, GUID_NULL, S_OK, sample.Get());
    return S_OK;
}

void VirtualCamMediaStream::QueueBlankSample(IUnknown* pToken)
{
    // Deliver a real all-zero NV12 frame at the negotiated size. A
    // buffer-less sample is ignored by DSHOW clients, which would then stall
    // waiting for data; an actual black frame keeps them running and is
    // replaced by real frames as soon as the companion catches up.
    UINT32 w = _curW ? _curW : 1920;
    UINT32 h = _curH ? _curH : 1080;
    DWORD frameLen = w * h * 3 / 2;
    if (frameLen > 1920 * 1080 * 3 / 2)  // sanity: never exceed the mapping
        frameLen = 1920 * 1080 * 3 / 2;

    Microsoft::WRL::ComPtr<IMFSample> sample;
    if (FAILED(MFCreateSample(&sample)))
        return;

    Microsoft::WRL::ComPtr<IMFMediaBuffer> buffer;
    if (FAILED(MFCreateMemoryBuffer(frameLen, &buffer)))
        return;

    BYTE* dst = nullptr;
    if (FAILED(buffer->Lock(&dst, nullptr, nullptr)))
        return;
    ZeroMemory(dst, frameLen);
    buffer->Unlock();
    buffer->SetCurrentLength(frameLen);

    sample->AddBuffer(buffer.Get());
    sample->SetSampleTime(MFGetSystemTime());
    sample->SetSampleDuration(333333); // ~30 fps in 100-nanosecond units

    if (pToken)
        sample->SetUnknown(MFSampleExtension_Token, pToken);
    _eventQueue->QueueEventParamUnk(MEMediaSample, GUID_NULL, S_OK, sample.Get());
}

void VirtualCamMediaStream::Shutdown()
{
    _eventQueue->Shutdown();
}

STDMETHODIMP VirtualCamMediaStream::GetEvent(DWORD dwFlags, IMFMediaEvent** ppEvent)
{
    return _eventQueue->GetEvent(dwFlags, ppEvent);
}

STDMETHODIMP VirtualCamMediaStream::BeginGetEvent(IMFAsyncCallback* pCallback, IUnknown* punkState)
{
    return _eventQueue->BeginGetEvent(pCallback, punkState);
}

STDMETHODIMP VirtualCamMediaStream::EndGetEvent(IMFAsyncResult* pResult, IMFMediaEvent** ppEvent)
{
    return _eventQueue->EndGetEvent(pResult, ppEvent);
}

STDMETHODIMP VirtualCamMediaStream::QueueEvent(MediaEventType met, REFGUID guidExtendedType, HRESULT hrStatus, const PROPVARIANT* pvValue)
{
    return _eventQueue->QueueEventParamVar(met, guidExtendedType, hrStatus, pvValue);
}
