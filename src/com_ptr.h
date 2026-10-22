#pragma once
#ifdef _WIN32
#include <wrl/client.h>
#else
#include <unknwn.h>
#include <utility>
#include <cassert>
namespace Microsoft::WRL {
template<class T> class ComPtr {
    T* value_=nullptr;
public:
    ComPtr()=default;
    ComPtr(std::nullptr_t) {}
    ComPtr(T* value):value_(value) {if(value_) value_->AddRef();}
    ComPtr(const ComPtr& other):ComPtr(other.value_) {}
    ComPtr(ComPtr&& other) noexcept:value_(other.Detach()) {}
    ~ComPtr() {Reset();}
    ComPtr& operator=(ComPtr other) noexcept {std::swap(value_,other.value_);return *this;}
    T* Get() const {return value_;}
    T* operator->() const {assert(value_);return value_;}
    explicit operator bool() const {return value_!=nullptr;}
    T** GetAddressOf() {return &value_;}
    T* const* GetAddressOf() const {return &value_;}
    T** ReleaseAndGetAddressOf() {Reset();return &value_;}
    T** operator&() {return ReleaseAndGetAddressOf();}
    void Reset() {if(auto* old=std::exchange(value_,nullptr)) old->Release();}
    T* Detach() {return std::exchange(value_,nullptr);}
    void Attach(T* value) {Reset();value_=value;}
    void Swap(ComPtr& other) noexcept {std::swap(value_,other.value_);}
    template<class U> HRESULT As(U** output) const {
        if(!value_) {*output=nullptr;return E_NOINTERFACE;}
        return value_->QueryInterface(__uuidof(U),reinterpret_cast<void**>(output));
    }
};
}
#endif
