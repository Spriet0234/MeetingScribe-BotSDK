#include <iostream>
#include <thread>
#include <chrono>
#include <condition_variable>
#include <mutex>
#include <curl/curl.h>
#include <nlohmann/json.hpp>
#include <glib.h>

//#include "audio_recorder.h"
#include "zoom_sdk.h"
#include "auth_service_interface.h"
#include "meeting_service_interface.h"

using namespace ZOOMSDK;
using json = nlohmann::json;

// ─── Globals ─────────────────────────────────────────────────────────────────────
std::mutex auth_mutex;
std::condition_variable auth_cv;
bool auth_done = false;
GMainLoop* authLoop = nullptr;
static std::string g_meetingId;
static std::string passcodeGlobal;  // holds command-line passcode
static std::string zakGlobal;       // holds command-line ZAK token if any
static IMeetingService* meetingService = nullptr;
static IAuthService* authService = nullptr;

// ─── Helper: fetch JWT ─────────────────────────────────────────────────────────────
static size_t WriteCallback(void* contents, size_t size, size_t nmemb, void* userp) {
    auto* str = static_cast<std::string*>(userp);
    str->append(static_cast<char*>(contents), size * nmemb);
    return size * nmemb;
}

std::string fetchJwtToken() {
    CURL* curl = curl_easy_init();
    std::string response;
    if (curl) {
        curl_easy_setopt(curl, CURLOPT_URL, "https://meeting-scribe-backend.vercel.app/api/jwt");
        curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, WriteCallback);
        curl_easy_setopt(curl, CURLOPT_WRITEDATA, &response);
        CURLcode res = curl_easy_perform(curl);
        if (res != CURLE_OK) {
            std::cerr << "❌ curl error: " << curl_easy_strerror(res) << std::endl;
        }
        curl_easy_cleanup(curl);
    }
    try {
        auto j = json::parse(response);
        return j.at("token").get<std::string>();
    } catch (...) {
        std::cerr << "❌ Failed to parse JWT response." << std::endl;
        return "";
    }
}

// ─── Meeting Event Handler ────────────────────────────────────────────────────────
class MyMeetingEventHandler : public IMeetingServiceEvent {
public:
    MyMeetingEventHandler() : quitCalled(false) {}
    void onMeetingStatusChanged(MeetingStatus status, int result) override {
        switch (status) {
            case MEETING_STATUS_CONNECTING:
                std::cout << "⏳ Connecting to meeting..." << std::endl;
                break;
            case MEETING_STATUS_INMEETING:
                std::cout << "🎉 Joined meeting!" << std::endl;
		//startAudioRecording();
                break;
            case MEETING_STATUS_DISCONNECTING:
            case MEETING_STATUS_FAILED:
                if (!quitCalled) {
                    std::cout << "🛑 Meeting ended (status=" << status << ") reason=" << result << std::endl;
		   //stopAudioRecording();
                    if (authLoop) g_main_loop_quit(authLoop);
                    quitCalled = true;
                }
                break;
            default:
                break;
        }
    }
    void onMeetingStatisticsWarningNotification(StatisticsWarningType) override {}
    void onMeetingParameterNotification(const MeetingParameter*) override {}
    void onSuspendParticipantsActivities() override {}
    void onAICompanionActiveChangeNotice(bool) override {}
    void onMeetingTopicChanged(const zchar_t*) override {}
    void onMeetingFullToWatchLiveStream(const zchar_t*) override {}
private:
    bool quitCalled;
};
static MyMeetingEventHandler meetingHandler;

// ─── Join Helper ─────────────────────────────────────────────────────────────────
void joinMeeting(const std::string& meetingId,
                 const std::string& userName,
                 const std::string& passcode = "",
                 const std::string& zakToken = "")
{
    if (CreateMeetingService(&meetingService) != SDKERR_SUCCESS || !meetingService) {
        std::cerr << "Failed to create MeetingService" << std::endl;
        return;
    }
    meetingService->SetEvent(&meetingHandler);

    JoinParam jp;
    jp.userType = SDK_UT_WITHOUT_LOGIN;
    auto& p = jp.param.withoutloginuserJoin;
    p.meetingNumber       = std::stoull(meetingId);
    p.vanityID            = nullptr;
    p.userName            = userName.c_str();
    p.psw                 = passcode.c_str();
    p.app_privilege_token = nullptr;
    p.userZAK             = zakToken.empty() ? nullptr : zakToken.c_str();
    p.customer_key        = nullptr;
    p.webinarToken        = nullptr;
    p.isVideoOff          = false;
    p.isAudioOff          = false;
    p.join_token          = nullptr;
    p.onBehalfToken       = nullptr;
    p.isMyVoiceInMix      = false;
    p.isAudioRawDataStereo = false;
    p.eAudioRawdataSamplingRate = AudioRawdataSamplingRate_32K;

    SDKError err = meetingService->Join(jp);
    if (err != SDKERR_SUCCESS) {
        std::cerr << "Join failed: " << err << std::endl;
    }
}

// ─── Auth Event Handler ───────────────────────────────────────────────────────────
class MyAuthEventHandler : public IAuthServiceEvent {
public:
    void onAuthenticationReturn(AuthResult result) override {
        if (result == AUTHRET_SUCCESS) {
            std::cout << "✅ Auth success – joining " << g_meetingId << std::endl;
            joinMeeting(g_meetingId, "MyBot", passcodeGlobal, zakGlobal);
        } else {
            std::cerr << "❌ Auth failed: " << result << std::endl;
            if (authLoop) g_main_loop_quit(authLoop);
        }
    }
    void onLogout() override {}
    void onZoomIdentityExpired() override {}
    void onZoomAuthIdentityExpired() override {}
    void onLoginReturnWithReason(LOGINSTATUS, IAccountInfo*, LoginFailReason) override {}
};

// ─── main() ───────────────────────────────────────────────────────────────────────
int main(int argc, char* argv[]) {
    if (argc < 2) {
        std::cerr << "Usage: zoom_bot <meetingNumber> [passcode] [zakToken]" << std::endl;
        return 1;
    }
    g_meetingId = argv[1];
    if (argc >= 3) passcodeGlobal = argv[2];
    if (argc >= 4) zakGlobal = argv[3];

    auto jwtToken = fetchJwtToken();
    if (jwtToken.empty()) {
        std::cerr << "Failed to fetch JWT" << std::endl;
        return -1;
    }
    std::cout << "🚀 Using JWT Token:\n" << jwtToken << std::endl;

    // Init SDK
    InitParam ip;
    ip.strWebDomain       = "https://zoom.us";
    ip.strSupportUrl      = "https://zoom.us";
    ip.emLanguageID       = LANGUAGE_English;
    ip.enableLogByDefault = true;
    ip.enableGenerateDump = true;
    if (InitSDK(ip) != SDKERR_SUCCESS) {
        std::cerr << "SDK init failed" << std::endl;
        return -1;
    }

    // Auth
    if ( CreateAuthService(&authService) != SDKERR_SUCCESS || !authService ) {
        std::cerr << "Failed to create AuthService" << std::endl;
        return -1;
    }
    static MyAuthEventHandler authHandler;
    authService->SetEvent(&authHandler);

    AuthContext ac;
    ac.jwt_token = jwtToken.c_str();
    if (authService->SDKAuth(ac) != SDKERR_SUCCESS) {
        std::cerr << "SDKAuth failed" << std::endl;
        return -1;
    }

    // Run GLib loop until DISCONNECTING/FAILED
    authLoop = g_main_loop_new(nullptr, FALSE);
    g_main_loop_run(authLoop);
    g_main_loop_unref(authLoop);

    CleanUPSDK();
    return 0;
}

