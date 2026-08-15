// ============================================================================
// MediaStream.h — IMFMediaStream implementation
// Delivers samples (video frames) to the Media Foundation pipeline.
// ============================================================================
#pragma once
#include <windows.h>

#ifndef STATUS_WAIT_0
#define STATUS_WAIT_0 ((DWORD)0x00000000L)
#endif
#ifndef STATUS_ABANDONED_WAIT_0
#define STATUS_ABANDONED_WAIT_0 ((DWORD)0x00000080L)
#endif

#include <mfidl.h>
#include <wrl/client.h>
#include <wrl/implements.h>
#include <memory>
#include <vector>
#include "FrameServer.h"

class VirtualCamMediaSource;

class VirtualCamMediaStream :
    public Microsoft::WRL::RuntimeClass<
        Microsoft::WRL::RuntimeClassFlags<Microsoft::WRL::ClassicCom>,
        IMFMediaStream,
        IMFMediaEventGenerator>
{
public:
    VirtualCamMediaStream();
    ~VirtualCamMediaStream();

    HRESULT RuntimeClassInitialize(VirtualCamMediaSource* pSource);
    void SetActive(bool active);
    void Shutdown();
    HRESULT SetCurrentMediaTypeOnHandler();
    HRESULT FireStreamStarted(const PROPVARIANT* pvarStartPosition);
    void SyncDesiredResolution();
    HRESULT GetNegotiatedType(IMFMediaType** ppType);
    HRESULT SetNegotiatedType(IMFMediaType* pType);
    void SetStreamDescriptor(IMFStreamDescriptor* pDescriptor);

    // IMFMediaStream
    STDMETHODIMP GetMediaSource(IMFMediaSource** ppMediaSource) override;
    STDMETHODIMP GetStreamDescriptor(IMFStreamDescriptor** ppStreamDescriptor) override;
    STDMETHODIMP RequestSample(IUnknown* pToken) override;

    // IMFMediaEventGenerator
    STDMETHODIMP GetEvent(DWORD dwFlags, IMFMediaEvent** ppEvent) override;
    STDMETHODIMP BeginGetEvent(IMFAsyncCallback* pCallback, IUnknown* punkState) override;
    STDMETHODIMP EndGetEvent(IMFAsyncResult* pResult, IMFMediaEvent** ppEvent) override;
    STDMETHODIMP QueueEvent(MediaEventType met, REFGUID guidExtendedType, HRESULT hrStatus, const PROPVARIANT* pvValue) override;

private:
    HRESULT CreateMediaType(UINT32 width, UINT32 height, IMFMediaType** ppMediaType);
    void QueueBlankSample(IUnknown* pToken);

    Microsoft::WRL::ComPtr<IMFMediaEventQueue> _eventQueue;
    Microsoft::WRL::ComPtr<IMFStreamDescriptor> _streamDescriptor;
    Microsoft::WRL::ComPtr<VirtualCamMediaSource> _source;
    
    std::unique_ptr<FrameServer> _frameServer;
    bool _active;
    UINT64 _lastFrameIndex;
    UINT32 _curW = 0;  // negotiated media type size (from SyncDesiredResolution)
    UINT32 _curH = 0;
    DWORD _lastFrameLen = 0;  // valid bytes in _lastFrame (max-size buffer)
    std::vector<BYTE> _lastFrame; // Caches the last frame to simulate freezing on disconnect
};
