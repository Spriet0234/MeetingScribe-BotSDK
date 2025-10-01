#include <iostream>
#include <thread>
#include <chrono>
#include <condition_variable>
#include <mutex>
#include <curl/curl.h>
#include <nlohmann/json.hpp>
#include <glib.h>
#include <fstream>
#include <atomic>
#include <vector>

// Zoom SDK
#include "zoom_sdk.h"
#include "auth_service_interface.h"
#include "meeting_service_interface.h"
#include "meeting_service_components/meeting_recording_interface.h"
#include "meeting_service_components/meeting_audio_interface.h"
#include "meeting_service_components/meeting_participants_ctrl_interface.h"

// Raw data
#include "rawdata/zoom_rawdata_api.h"
#include "rawdata/rawdata_audio_helper_interface.h"
#include "zoom_sdk_raw_data_def.h"

// socket
#include <netinet/in.h>
#include <arpa/inet.h>
#include <unistd.h>

#include <codecvt>
#include <locale>
#include <unordered_map>
#include <string>

using namespace ZOOMSDK;
using json = nlohmann::json;

// ─── Globals ─────────────────────────────────────────────────────────────────────
std::mutex auth_mutex;
std::condition_variable auth_cv;
bool auth_done = false;
GMainLoop *authLoop = nullptr;

static std::string g_meetingId;
static std::string passcodeGlobal;
static std::string zakGlobal;

static IMeetingService *meetingService = nullptr;
static IAuthService *authService = nullptr;
static IMeetingRecordingController *g_recordController = nullptr;
static IZoomSDKAudioRawDataHelper *g_audioHelper = nullptr;

static bool g_quiet = false;

static std::atomic<uint64_t> g_mixedFrames{0};
static std::atomic<bool> g_audioSubscribed{false};
static std::atomic<bool> g_rawRecording{false};

static std::atomic<bool> g_roster_refresh_running{false};

static std::unordered_map<unsigned int, std::string> g_uid_to_name;

static int g_pcm_sock = -1;

static std::atomic<bool> g_sdkReady{false};
static std::atomic<bool> g_inMeeting{false};
static std::atomic<bool> g_idleMode{false};

static std::mutex g_job_mx;
static std::condition_variable g_job_cv;
static std::string g_next_meeting_id;
static std::string g_next_passcode;
static std::string g_next_zak;

static std::string zchar_to_utf8(const zchar_t *z)
{
    if (!z)
        return "[unknown]";

#if defined(_WIN32) || defined(_WIN64)
    try
    {
        std::wstring ws(reinterpret_cast<const wchar_t *>(z));
        static std::wstring_convert<std::codecvt_utf8_utf16<wchar_t>> conv;
        return conv.to_bytes(ws);
    }
    catch (...)
    {
        return "[invalid-name]";
    }
#else
    try
    {
        return std::string(reinterpret_cast<const char *>(z));
    }
    catch (...)
    {
        return "[invalid-name]";
    }
#endif
}

static void send_udp_line(uint16_t port, const std::string &line)
{
    int s = socket(AF_INET, SOCK_DGRAM, 0);
    if (s < 0)
        return;
    sockaddr_in a{};
    a.sin_family = AF_INET;
    a.sin_port = htons(port);
    inet_pton(AF_INET, "127.0.0.1", &a.sin_addr);
    sendto(s, line.data(), (int)line.size(), 0, (sockaddr *)&a, sizeof(a));
    close(s);
}

static inline void info(const std::string &msg)
{
    if (!g_quiet)
        std::cout << msg << std::endl;
}
static inline void error(const std::string &msg)
{
    if (!g_quiet)
        std::cerr << msg << std::endl;
}

static void control_listener_thread()
{
    int srv = socket(AF_INET, SOCK_STREAM, 0);
    if (srv < 0)
    {
        error("[idle] socket() failed");
        return;
    }
    int opt = 1;
    setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(7600);
    // bind to all interfaces so host -> -p 7600:7600 always reaches us
    addr.sin_addr.s_addr = htonl(INADDR_ANY);

    if (bind(srv, (sockaddr *)&addr, sizeof(addr)) != 0)
    {
        perror("[idle] bind 0.0.0.0:7600");
        close(srv);
        return;
    }
    if (listen(srv, 16) != 0)
    {
        perror("[idle] listen");
        close(srv);
        return;
    }
    info("[idle] control listener on 0.0.0.0:7600");

    auto send_all = [](int fd, const std::string &s)
    {
        const char *p = s.data();
        size_t n = s.size();
        while (n)
        {
            ssize_t w = send(fd, p, n, MSG_NOSIGNAL);
            if (w <= 0)
                break;
            p += w;
            n -= size_t(w);
        }
    };

    while (true)
    {
        sockaddr_in peer{};
        socklen_t plen = sizeof(peer);
        int c = accept(srv, (sockaddr *)&peer, &plen);
        if (c < 0)
            continue;

        char peerip[64];
        inet_ntop(AF_INET, &peer.sin_addr, peerip, sizeof(peerip));
        info(std::string("[idle] accept from ") + peerip + ":" + std::to_string(ntohs(peer.sin_port)));

        std::string buf;
        char tmp[1024];
        // read one line
        while (true)
        {
            ssize_t r = recv(c, tmp, sizeof(tmp), 0);
            if (r <= 0)
                break;
            buf.append(tmp, tmp + r);
            auto pos = buf.find('\n');
            if (pos != std::string::npos)
            {
                buf.resize(pos);
                break;
            }
            if (buf.size() > 64 * 1024)
                break;
        }
        info(std::string("[idle] received: ") + buf);

        try
        {
            auto j = json::parse(buf);
            std::string mid = j.at("meeting_id").get<std::string>();
            std::string pwd = j.value("passcode", "");
            std::string zak = j.value("zak", "");
            if (!g_sdkReady.load())
            {
                send_all(c, "ERR not_ready\n");
                info("[idle] replied: ERR not_ready");
            }
            else if (g_inMeeting.load())
            {
                send_all(c, "ERR busy\n");
                info("[idle] replied: ERR busy");
            }
            else
            {
                {
                    std::lock_guard<std::mutex> lk(g_job_mx);
                    g_next_meeting_id = mid;
                    g_next_passcode = pwd;
                    g_next_zak = zak;
                }
                g_job_cv.notify_one();
                send_all(c, "OK queued\n");
                info(std::string("[idle] queued job for meeting ") + mid);
            }
        }
        catch (const std::exception &e)
        {
            send_all(c, "ERR bad_json\n");
            info(std::string("[idle] replied: ERR bad_json: ") + e.what());
        }
        catch (...)
        {
            send_all(c, "ERR bad_json\n");
            info("[idle] replied: ERR bad_json (unknown)");
        }
        close(c);
    }
}

class WavWriter
{
public:
    bool open(const std::string &path, uint32_t sampleRate, uint16_t channels)
    {
        close();
        sample_rate_ = sampleRate;
        channels_ = channels;
        ofs_.open(path, std::ios::binary);
        if (!ofs_)
            return false;
        writeHeaderPlaceholder();
        return true;
    }
    void write(const char *data, size_t len)
    {
        if (!ofs_)
            return;
        ofs_.write(data, static_cast<std::streamsize>(len));
        data_bytes_ += static_cast<uint32_t>(len);
    }
    void close()
    {
        if (ofs_)
            finalizeHeader();
        if (ofs_.is_open())
            ofs_.close();
        data_bytes_ = 0;
    }
    ~WavWriter() { close(); }

private:
    std::ofstream ofs_;
    uint32_t data_bytes_ = 0;
    uint32_t sample_rate_ = 32000;
    uint16_t channels_ = 1;

    void writeHeaderPlaceholder()
    {
        uint16_t audio_format = 1, bits_per_sample = 16;
        uint32_t byte_rate = sample_rate_ * channels_ * bits_per_sample / 8;
        uint16_t block_align = channels_ * bits_per_sample / 8;
        ofs_.write("RIFF", 4);
        writeLE32(0);
        ofs_.write("WAVE", 4);
        ofs_.write("fmt ", 4);
        writeLE32(16);
        writeLE16(audio_format);
        writeLE16(channels_);
        writeLE32(sample_rate_);
        writeLE32(byte_rate);
        writeLE16(block_align);
        writeLE16(bits_per_sample);
        ofs_.write("data", 4);
        writeLE32(0);
    }
    void finalizeHeader()
    {
        if (!ofs_)
            return;
        auto cur = ofs_.tellp();
        ofs_.seekp(40, std::ios::beg);
        writeLE32(data_bytes_);
        ofs_.seekp(4, std::ios::beg);
        writeLE32(36 + data_bytes_);
        ofs_.seekp(cur, std::ios::beg);
    }
    void writeLE16(uint16_t v)
    {
        char b[2] = {char(v & 0xFF), char((v >> 8) & 0xFF)};
        ofs_.write(b, 2);
    }
    void writeLE32(uint32_t v)
    {
        char b[4] = {char(v & 0xFF), char((v >> 8) & 0xFF), char((v >> 16) & 0xFF), char((v >> 24) & 0xFF)};
        ofs_.write(b, 4);
    }
};

static std::unique_ptr<WavWriter> g_wav;

static void open_pcm_socket_once()
{
    if (g_pcm_sock != -1)
        return;
    g_pcm_sock = socket(AF_INET, SOCK_STREAM, 0);
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(7000); // BOT_PCM_PORT
    inet_pton(AF_INET, "127.0.0.1", &addr.sin_addr);
    if (connect(g_pcm_sock, (sockaddr *)&addr, sizeof(addr)) != 0)
    {
        perror("connect 127.0.0.1:7000");
        g_pcm_sock = -1;
    }
}

static void emit_roster_names(IMeetingService *ms)
{
    if (!ms)
        return;

    auto pc = ms->GetMeetingParticipantsController();
    if (!pc)
    {
        info("[roster] participants controller is null");
        return;
    }

    auto lst = pc->GetParticipantsList();
    if (!lst)
    {
        info("[roster] participant list is null");
        return;
    }

    for (int i = 0; i < lst->GetCount(); ++i)
    {
        unsigned int uid = lst->GetItem(i);
        auto infoI = pc->GetUserByUserID(uid);
        std::string name = "[unknown]";
        if (infoI)
        {
            const zchar_t *zname = infoI->GetUserName();
            auto n = zchar_to_utf8(zname);
            if (!n.empty())
                name = n;
        }
        g_uid_to_name[uid] = name;
    }

    std::string line = "[roster] ";
    for (const auto &kv : g_uid_to_name)
        line += std::to_string(kv.first) + ":" + kv.second + "  ";
    info(line);

    auto sanitize = [](std::string s)
    {
        for (char &c : s)
        {
            if (c == '|' || c == ':' || c == '\n' || c == '\r')
                c = ' ';
        }
        return s;
    };

    std::string wire = "map=";
    bool first = true;
    for (const auto &kv : g_uid_to_name)
    {
        if (!first)
            wire += '|';
        first = false;
        wire += std::to_string(kv.first) + ":" + sanitize(kv.second);
    }

    send_udp_line(7101, wire);
}

class MyAudioDelegate : public IZoomSDKAudioRawDataDelegate
{
public:
    void onMixedAudioRawDataReceived(AudioRawData *data) override
    {
        if (!data)
            return;

        open_pcm_socket_once();
        if (g_pcm_sock != -1)
        {
            send(g_pcm_sock, data->GetBuffer(), data->GetBufferLen(), MSG_NOSIGNAL);
        }

        if (g_wav)
            g_wav->write(data->GetBuffer(), data->GetBufferLen());

        auto count = ++g_mixedFrames;
        // if ((count % 50) == 0 && !g_quiet)
        // {
        //     std::cout << "[audio] mixed frames received: " << count
        //               << " (last chunk bytes=" << data->GetBufferLen() << ")\n";
        // }
    }

    void onOneWayAudioRawDataReceived(AudioRawData *, uint32_t) override {}
    void onShareAudioRawDataReceived(AudioRawData *, uint32_t) override {}
    void onOneWayInterpreterAudioRawDataReceived(AudioRawData *, const zchar_t *) override {}
};

static std::unique_ptr<MyAudioDelegate> g_audioDelegate;

class MyAudioCtrlEvent : public IMeetingAudioCtrlEvent
{
public:
    void onUserAudioStatusChange(IList<IUserAudioStatus *> *lst, const zchar_t *) override
    {
        if (!lst)
            return;
        for (int i = 0; i < lst->GetCount(); ++i)
        {
            auto *s = lst->GetItem(i);
            if (!s)
                continue;
            info("Audio status: uid=" + std::to_string(s->GetUserId()) +
                 ", type=" + std::to_string((int)s->GetAudioType()) +
                 ", status=" + std::to_string((int)s->GetStatus()));
        }
        emit_roster_names(meetingService);
    }
    void onUserActiveAudioChange(IList<unsigned int> *lst) override
    {
        if (!lst)
            return;

        // Log active UIDs
        std::string uids;
        for (int i = 0; i < lst->GetCount(); ++i)
        {
            if (i)
                uids += ',';
            uids += std::to_string(lst->GetItem(i));
        }
        info("Active audio: " + uids);

        // Send to Node on UDP 7100 as: active=uid,uid,uid
        send_udp_line(7100, std::string("active=") + uids);

        // Keep the name map fresh
        emit_roster_names(meetingService);
    }

    void onHostRequestStartAudio(IRequestStartAudioHandler *) override {}
    void onJoin3rdPartyTelephonyAudio(const zchar_t *) override {}
    void onMuteOnEntryStatusChange(bool) override {}
};

static std::unique_ptr<MyAudioCtrlEvent> g_audioCtrlEvent;

// ─── JWT helper ──────────────────────────────────────────────────────────────────
static size_t WriteCallback(void *c, size_t s, size_t n, void *u)
{
    auto *str = static_cast<std::string *>(u);
    str->append(static_cast<char *>(c), s * n);
    return s * n;
}
std::string fetchJwtToken()
{
    CURL *curl = curl_easy_init();
    std::string resp;
    if (curl)
    {
        curl_easy_setopt(curl, CURLOPT_URL, "https://meeting-scribe-backend.vercel.app/api/jwt");
        curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, WriteCallback);
        curl_easy_setopt(curl, CURLOPT_WRITEDATA, &resp);
        CURLcode res = curl_easy_perform(curl);
        if (res != CURLE_OK)
            std::cerr << "curl error: " << curl_easy_strerror(res) << std::endl;
        curl_easy_cleanup(curl);
    }
    try
    {
        return json::parse(resp).at("token").get<std::string>();
    }
    catch (...)
    {
        std::cerr << "Failed to parse JWT response.\n";
        return "";
    }
}

static void try_start_raw_pipeline()
{
    if (!meetingService)
        return;

    auto rec = meetingService->GetMeetingRecordingController();
    if (!rec)
    {
        error("No MeetingRecordingController");
        return;
    }

    SDKError r = rec->StartRawRecording();
    if (r == SDKERR_SUCCESS)
    {
        g_rawRecording = true;
        info("Raw recording started");
    }
    else if (r == SDKERR_NO_PERMISSION)
    {
        error("StartRawRecording: NO_PERMISSION (wait for host to grant local recording)");
        return;
    }
    else
    {
        error(std::string("StartRawRecording failed: ") + std::to_string(r));
        return;
    }

    g_audioHelper = GetAudioRawdataHelper();
    if (!g_audioHelper)
    {
        error("GetAudioRawdataHelper() returned null");
        return;
    }

    if (!g_audioDelegate)
        g_audioDelegate = std::make_unique<MyAudioDelegate>();
    if (!g_wav)
        g_wav = std::make_unique<WavWriter>();

    if (!g_wav->open("recording.wav", 32000, 1))
    {
        error("Failed to open recording.wav");
        return;
    }

    SDKError s = g_audioHelper->subscribe(g_audioDelegate.get());
    if (s == SDKERR_SUCCESS)
    {
        g_audioSubscribed = true;
        info("Subscribed to mixed audio raw data");
    }
    else
    {
        error(std::string("Audio subscribe failed: ") + std::to_string(s));
    }
}

struct MyRecordingCtrlEvent : public IMeetingRecordingCtrlEvent
{
    void onRecordingStatus(RecordingStatus status) override
    {
        info(std::string("[rec] local recording status=") + std::to_string((int)status));
    }
    void onCloudRecordingStatus(RecordingStatus status) override
    {
        info(std::string("[rec] cloud recording status=") + std::to_string((int)status));
    }
    void onRecordPrivilegeChanged(bool bCanRec) override
    {
        info(std::string("[rec] record privilege changed: ") + (bCanRec ? "granted" : "revoked"));
        if (bCanRec && !g_rawRecording.load())
            try_start_raw_pipeline();
    }
    void onLocalRecordingPrivilegeRequestStatus(RequestLocalRecordingStatus st) override
    {
        info(std::string("[rec] local rec privilege request status=") + std::to_string((int)st));
    }
    void onRequestCloudRecordingResponse(RequestStartCloudRecordingStatus st) override
    {
        info(std::string("[rec] request cloud recording response=") + std::to_string((int)st));
    }
    void onLocalRecordingPrivilegeRequested(IRequestLocalRecordingPrivilegeHandler *) override
    {
        info("[rec] local recording privilege requested (host-side)");
    }
    void onStartCloudRecordingRequested(IRequestStartCloudRecordingHandler *) override
    {
        info("[rec] start cloud recording requested (host-side)");
    }
    void onCloudRecordingStorageFull(time_t) override { error("[rec] cloud recording storage full"); }
#if defined(WIN32)
    void onRecording2MP4Done(bool, int, const zchar_t *) override {}
    void onRecording2MP4Processing(int) override {}
    void onCustomizedLocalRecordingSourceNotification(void *) override {}
#endif
    void onEnableAndStartSmartRecordingRequested(IRequestEnableAndStartSmartRecordingHandler *) override
    {
        info("[rec] smart recording enable/start requested");
    }
    void onSmartRecordingEnableActionCallback(ISmartRecordingEnableActionHandler *) override
    {
        info("[rec] smart recording enable action callback");
    }
#if defined(__linux__)
    void onTranscodingStatusChanged(TranscodingStatus st, const zchar_t *path) override
    {
        info(std::string("[rec] transcoding status=") + std::to_string((int)st) + (path ? " (path set)" : " (path null)"));
    }
#endif
};
static std::unique_ptr<MyRecordingCtrlEvent> g_recordEvt;

static void close_pcm_socket()
{
    if (g_pcm_sock != -1)
    {
        close(g_pcm_sock);
        g_pcm_sock = -1;
    }
}

class MyMeetingEventHandler : public IMeetingServiceEvent
{
public:
    MyMeetingEventHandler() : quitCalled(false) {}
    void onMeetingStatusChanged(MeetingStatus status, int result) override
    {
        auto rec = meetingService->GetMeetingRecordingController();
        if (rec)
        {
            if (!g_recordEvt)
                g_recordEvt = std::make_unique<MyRecordingCtrlEvent>();
            rec->SetEvent(g_recordEvt.get());
        }

        auto fail_code_to_str = [](int code) -> const char *
        {
            switch (code)
            {
            case 1:
                return "MEETING_FAIL_CONNECTION_ERR";
            case 2:
                return "MEETING_FAIL_RECONNECT_ERR";
            case 3:
                return "MEETING_FAIL_MMR_ERR";
            case 4:
                return "MEETING_FAIL_PASSWORD_ERR";
            case 6:
                return "MEETING_FAIL_MEETING_OVER";
            case 7:
                return "MEETING_FAIL_MEETING_NOT_START";
            case 8:
                return "MEETING_FAIL_MEETING_NOT_EXIST";
            case 9:
                return "MEETING_FAIL_MEETING_USER_FULL";
            case 10:
                return "MEETING_FAIL_CLIENT_INCOMPATIBLE";
            case 12:
                return "MEETING_FAIL_CONFLOCKED";
            case 13:
                return "MEETING_FAIL_MEETING_RESTRICTED";
            case 14:
                return "MEETING_FAIL_MEETING_RESTRICTED_JBH";
            case 15:
                return "MEETING_FAIL_CANNOT_EMIT_WEBREQUEST";
            case 16:
                return "MEETING_FAIL_CANNOT_START_TOKENEXPIRE";
            case 27:
                return "CONF_FAIL_VANITY_NOT_EXIST";
            case 29:
                return "CONF_FAIL_DISALLOW_HOST_MEETING";
            case 50:
                return "MEETING_FAIL_WRITE_CONFIG_FILE";
            case 60:
                return "MEETING_FAIL_FORBID_TO_JOIN_INTERNAL_MEETING";
            case 61:
                return "CONF_FAIL_REMOVED_BY_HOST";
            case 62:
                return "MEETING_FAIL_HOST_DISALLOW_OUTSIDE_USER_JOIN";
            case 63:
                return "MEETING_FAIL_UNABLE_TO_JOIN_EXTERNAL_MEETING";
            case 64:
                return "MEETING_FAIL_BLOCKED_BY_ACCOUNT_ADMIN";
            case 82:
                return "MEETING_FAIL_NEED_SIGN_IN_FOR_PRIVATE_MEETING";
            case 500:
                return "MEETING_FAIL_APP_PRIVILEGE_TOKEN_ERROR";
            default:
                return "UNKNOWN_FAIL_CODE";
            }
        };
        auto print_status = [&](const char *label)
        {
            if (!g_quiet)
                std::cout << label << " (status=" << status << ", code=" << result << ")\n";
        };

        switch (status)
        {
        case MEETING_STATUS_CONNECTING:
            g_inMeeting = true;
            print_status("Connecting to meeting...");
            break;

        case MEETING_STATUS_INMEETING:
            g_inMeeting = true;
            info("Joined meeting");
            if (meetingService)
            {
                if (auto audioCtrl = meetingService->GetMeetingAudioController())
                {
                    if (!g_audioCtrlEvent)
                        g_audioCtrlEvent = std::make_unique<MyAudioCtrlEvent>();
                    audioCtrl->SetEvent(g_audioCtrlEvent.get());
                    audioCtrl->EnablePlayMeetingAudio(true);
                    if (SDKError aerr = audioCtrl->JoinVoip(); aerr != SDKERR_SUCCESS)
                        error("JoinVoip failed: " + std::to_string(aerr));
                }

                g_recordController = meetingService->GetMeetingRecordingController();
                if (g_recordController)
                {
                    if (!g_recordEvt)
                        g_recordEvt = std::make_unique<MyRecordingCtrlEvent>();
                    g_recordController->SetEvent(g_recordEvt.get());
                    SDKError req = g_recordController->RequestLocalRecordingPrivilege();
                    if (req != SDKERR_SUCCESS)
                        error("RequestLocalRecordingPrivilege failed: " + std::to_string(req));
                }

                g_audioHelper = GetAudioRawdataHelper();
                if (!g_audioHelper)
                    error("GetAudioRawdataHelper() returned null");

                emit_roster_names(meetingService);

                if (!g_roster_refresh_running.exchange(true))
                {
                    std::thread([]()
                                {
                        while (true) {
                            std::this_thread::sleep_for(std::chrono::seconds(10));
                            if (!meetingService) break;
                            emit_roster_names(meetingService);
                        }
                        g_roster_refresh_running = false; })
                        .detach();
                }
            }
            break;

        case MEETING_STATUS_FAILED:
            if (!quitCalled)
            {
                error(std::string("Meeting failed: ") + fail_code_to_str(result) + " (code=" + std::to_string(result) + ")");
                if (auto last = GetZoomLastError())
                {
                    error(std::string("LastError: type=") + std::to_string((int)last->GetErrorType()) +
                          ", code=" + std::to_string((unsigned long long)last->GetErrorCode()) +
                          ", desc=" + (last->GetErrorDescription() ? last->GetErrorDescription() : ""));
                }
                stopRecordingAndCleanup();
                g_inMeeting = false;
                if (authLoop)
                    g_main_loop_quit(authLoop);
                quitCalled = true;
            }
            break;

        case MEETING_STATUS_DISCONNECTING:
        case MEETING_STATUS_ENDED:
            stopRecordingAndCleanup();
            g_inMeeting = false;
            if (authLoop)
                g_main_loop_quit(authLoop);
            break;

        case MEETING_STATUS_WAITINGFORHOST:
        case MEETING_STATUS_IN_WAITING_ROOM:
        case MEETING_STATUS_RECONNECTING:
            print_status("ℹ️ Status change");
            break;

        default:
            print_status("ℹ️ Status change");
            break;
        }
    }

    void onMeetingStatisticsWarningNotification(StatisticsWarningType) override {}
    void onMeetingParameterNotification(const MeetingParameter *) override {}
    void onSuspendParticipantsActivities() override {}
    void onAICompanionActiveChangeNotice(bool) override {}
    void onMeetingTopicChanged(const zchar_t *) override {}
    void onMeetingFullToWatchLiveStream(const zchar_t *) override {}

private:
    void stopRecordingAndCleanup()
    {
        if (g_audioHelper && g_audioSubscribed.load())
        {
            g_audioHelper->unSubscribe();
            g_audioSubscribed = false;
        }
        if (g_recordController && g_rawRecording.load())
        {
            g_recordController->StopRawRecording();
            g_rawRecording = false;
        }
        close_pcm_socket();

        if (g_wav)
        {
            g_wav->close();
            info("Saved recording to recording.wav");
        }
    }

    bool quitCalled;
};

static MyMeetingEventHandler meetingHandler;

// ─── Join helper ─────────────────────────────────────────────────────────────────
void joinMeeting(const std::string &meetingId, const std::string &userName,
                 const std::string &passcode = "", const std::string &zakToken = "")
{
    if (CreateMeetingService(&meetingService) != SDKERR_SUCCESS || !meetingService)
    {
        std::cerr << "Failed to create MeetingService\n";
        return;
    }
    meetingService->SetEvent(&meetingHandler);

    JoinParam jp;
    jp.userType = SDK_UT_WITHOUT_LOGIN;
    auto &p = jp.param.withoutloginuserJoin;
    p.meetingNumber = std::stoull(meetingId);
    p.vanityID = nullptr;
    p.userName = userName.c_str();
    p.psw = passcode.c_str();
    p.app_privilege_token = nullptr;
    p.userZAK = zakToken.empty() ? nullptr : zakToken.c_str();
    p.customer_key = nullptr;
    p.webinarToken = nullptr;
    p.isVideoOff = true;
    p.isAudioOff = true;
    p.join_token = nullptr;
    p.onBehalfToken = nullptr;
    p.isMyVoiceInMix = false;
    p.isAudioRawDataStereo = false;
    p.eAudioRawdataSamplingRate = AudioRawdataSamplingRate_32K;

    SDKError err = meetingService->Join(jp);
    if (err != SDKERR_SUCCESS)
        std::cerr << "Join failed: " << err << std::endl;
}

class MyAuthEventHandler : public IAuthServiceEvent
{
public:
    void onAuthenticationReturn(AuthResult result) override
    {
        if (result == AUTHRET_SUCCESS)
        {
            g_sdkReady = true;
            if (!g_idleMode.load() && !g_meetingId.empty())
            {
                std::cout << "✅ Auth success – joining " << g_meetingId << std::endl;
                joinMeeting(g_meetingId, "MyBot", passcodeGlobal, zakGlobal);
            }
            else
            {
                info("✅ Auth success – idle mode active, waiting for meeting credentials");
            }
        }
        else
        {
            std::cerr << "❌ Auth failed: " << result << std::endl;
            if (authLoop)
                g_main_loop_quit(authLoop);
        }
    }
    void onLogout() override {}
    void onZoomIdentityExpired() override {}
    void onZoomAuthIdentityExpired() override {}
    void onLoginReturnWithReason(LOGINSTATUS, IAccountInfo *, LoginFailReason) override {}
};
int main(int argc, char *argv[])
{
    bool want_help = false;
    for (int i = 1; i < argc; ++i)
    {
        std::string a = argv[i];
        if (a == "--quiet" || a == "-q")
        {
            g_quiet = true;
            continue;
        }
        if (a == "--idle")
        {
            g_idleMode = true;
            continue;
        }
        if (a == "--help" || a == "-h")
        {
            want_help = true;
            continue;
        }
        // positional collection stays for non-idle mode
        if (a.size())
        {
            // keep original behavior
            // note: we will parse positionals after flag scan
        }
    }

    // rebuild positional (to preserve your original parsing)
    std::vector<std::string> positional;
    for (int i = 1; i < argc; ++i)
    {
        std::string a = argv[i];
        if (a == "--quiet" || a == "-q" || a == "--idle" || a == "--help" || a == "-h")
            continue;
        positional.push_back(a);
    }

    if (want_help)
    {
        std::cerr << "Usage: zoom_bot [--quiet|-q] [--idle] <meetingNumber> [passcode] [zakToken]\n";
        std::cerr << "       --idle = start authenticated and wait for JSON on 127.0.0.1:7600\n";
        return 0;
    }

    // In non-idle mode we still require a meeting id (keep your current UX)
    if (!g_idleMode.load())
    {
        if (positional.size() < 1)
        {
            std::cerr << "Usage: zoom_bot [--quiet|-q] <meetingNumber> [passcode] [zakToken]\n";
            return 1;
        }
        g_meetingId = positional[0];
        if (positional.size() >= 2)
            passcodeGlobal = positional[1];
        if (positional.size() >= 3)
            zakGlobal = positional[2];
    }

    auto jwtToken = fetchJwtToken();
    if (jwtToken.empty())
    {
        std::cerr << "Failed to fetch JWT\n";
        return -1;
    }
    info("Fetched JWT");

    InitParam ip;
    ip.strWebDomain = "https://zoom.us";
    ip.strSupportUrl = "https://zoom.us";
    ip.emLanguageID = LANGUAGE_English;
    ip.enableLogByDefault = true;
    ip.enableGenerateDump = true;
    ip.rawdataOpts.audioRawdataMemoryMode = ZoomSDKRawDataMemoryModeHeap;
    ip.rawdataOpts.videoRawdataMemoryMode = ZoomSDKRawDataMemoryModeHeap;
    ip.rawdataOpts.shareRawdataMemoryMode = ZoomSDKRawDataMemoryModeHeap;
    if (InitSDK(ip) != SDKERR_SUCCESS)
    {
        std::cerr << "SDK init failed\n";
        return -1;
    }

    if (!HasRawdataLicense())
        error("Raw data license not detected. Audio callbacks may not fire.");
    else
        info("Raw data license detected");

    if (CreateAuthService(&authService) != SDKERR_SUCCESS || !authService)
    {
        std::cerr << "Failed to create AuthService\n";
        return -1;
    }
    static MyAuthEventHandler authHandler;
    authService->SetEvent(&authHandler);

    AuthContext ac;
    ac.jwt_token = jwtToken.c_str();
    if (authService->SDKAuth(ac) != SDKERR_SUCCESS)
    {
        std::cerr << "SDKAuth failed\n";
        return -1;
    }

    // If idle mode, spin up control listener + a waiter that joins when a job arrives
    std::thread ctl;
    if (g_idleMode.load())
    {
        ctl = std::thread(control_listener_thread);

        std::thread waiter([]()
                           {
            // Wait for auth first
            while (!g_sdkReady.load()) std::this_thread::sleep_for(std::chrono::milliseconds(50));
            info("[idle] ready for jobs");

            while (true) {
                std::unique_lock<std::mutex> lk(g_job_mx);
                g_job_cv.wait(lk, []{ return !g_next_meeting_id.empty(); });
                std::string mid = g_next_meeting_id;
                std::string pwd = g_next_passcode;
                std::string zak = g_next_zak;
                g_next_meeting_id.clear(); g_next_passcode.clear(); g_next_zak.clear();
                lk.unlock();

                info("[idle] received job – joining " + mid);
                joinMeeting(mid, "MyBot", pwd, zak);

                // Wait until meeting finishes before accepting another
                while (g_inMeeting.load()) std::this_thread::sleep_for(std::chrono::milliseconds(200));
                info("[idle] meeting complete – ready for next job");
            } });
        waiter.detach();
    }

    authLoop = g_main_loop_new(nullptr, FALSE);
    g_main_loop_run(authLoop);
    g_main_loop_unref(authLoop);

    CleanUPSDK();

    if (ctl.joinable())
        ctl.detach();
    return 0;
}
