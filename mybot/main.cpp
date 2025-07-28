#include <iostream>
#include <thread>
#include <chrono>

#include "zoom_sdk.h"
#include "auth_service_interface.h"
#include "zoom_sdk_def.h"

#define ZOOM_JWT_TOKEN "YOUR_GENERATED_JWT_HERE"

using namespace ZOOMSDK;

int main() {
    // Step 1: Initialize SDK
    InitParam initParam;
    initParam.strWebDomain = "https://zoom.us";
    initParam.strSupportUrl = "https://zoom.us";
    initParam.emLanguageID = LANGUAGE_English;
    initParam.enableLogByDefault = true;
    initParam.enableGenerateDump = true;

    SDKError initErr = InitSDK(initParam);
    if (initErr != SDKERR_SUCCESS) {
        std::cerr << " SDK initialization failed: " << initErr << std::endl;
        return -1;
    }
    std::cout << "SDK initialized." << std::endl;

    // Step 2: Create AuthService
    IAuthService* authService = nullptr;
    if (CreateAuthService(&authService) != SDKERR_SUCCESS || !authService) {
        std::cerr << "Failed to create AuthService." << std::endl;
        return -1;
    }

    // Step 3: Setup AuthContext
    AuthContext authContext;
    authContext.jwt_token = ZOOM_JWT_TOKEN;

    SDKError authErr = authService->SDKAuth(authContext);
    if (authErr != SDKERR_SUCCESS) {
        std::cerr << "SDKAuth failed: " << authErr << std::endl;
        return -1;
    }

    std::cout << "Waiting for auth callback..." << std::endl;

    // Step 4: Wait (simulating event loop)
    std::this_thread::sleep_for(std::chrono::seconds(5));

    // Step 5: Cleanup
    CleanUPSDK();
    std::cout << "SDK cleanup complete." << std::endl;

    return 0;
}

