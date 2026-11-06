#include "mod_scripts.h"
#include "target.h"
extern "C" {
#include <lua.h>
#include <lauxlib.h>
#include <lualib.h>
}
#include <algorithm>
#include <array>
#include <chrono>
#include <cstdlib>
#include <format>
#include <memory>
#include <utility>

namespace b2 {
namespace {
constexpr std::size_t memory_limit=4*1024*1024,total_memory_limit=64*1024*1024;
constexpr unsigned instruction_limit=100000,event_instruction_limit=10000,hook_interval=100,max_messages=16;
struct MemoryBudget {std::size_t used=0;};
struct Context {
    struct Message {std::array<char,257> text{};bool on_screen=false;};
    std::array<Message,max_messages> messages{};
    unsigned message_count=0,current_fov=100,remaining=instruction_limit,limit=instruction_limit;
    std::optional<unsigned> requested_fov;
    std::size_t used=0,peak=0;
    MemoryBudget* memory=nullptr;
    int environment=LUA_NOREF;
    std::array<int,unsigned(ScriptEventType::count)> handlers;
    bool registering=true;
    ScriptEvent event;
    std::chrono::steady_clock::time_point deadline;
    Context() {handlers.fill(LUA_NOREF);}
    unsigned subscriptions() const {
        unsigned mask=0;for(unsigned i=0;i<handlers.size();++i) if(handlers[i]!=LUA_NOREF) mask|=1U<<i;
        return mask;
    }
};
Context& context(lua_State* state) {return **static_cast<Context**>(lua_getextraspace(state));}
void* allocate(void* user,void* pointer,std::size_t old_size,std::size_t new_size) {
    auto& c=*static_cast<Context*>(user);
    if(!pointer) old_size=0;
    if(!new_size) {std::free(pointer);c.used-=old_size;c.memory->used-=old_size;return nullptr;}
    if(new_size>memory_limit-(c.used-old_size) || new_size>total_memory_limit-(c.memory->used-old_size)) return nullptr;
    void* result=std::realloc(pointer,new_size);
    if(result) {c.used=c.used-old_size+new_size;c.memory->used=c.memory->used-old_size+new_size;c.peak=std::max(c.peak,c.used);}
    return result;
}
void budget(lua_State* state,lua_Debug*) {
    auto& c=context(state);
    if(c.remaining<=hook_interval) luaL_error(state,"script exceeded %d instructions",int(c.limit));
    c.remaining-=hook_interval;
    if(std::chrono::steady_clock::now()>c.deadline) luaL_error(state,"script exceeded its time limit");
}
int get_fov(lua_State* state) {
    auto& c=context(state);lua_pushinteger(state,c.requested_fov.value_or(c.current_fov));return 1;
}
int set_fov(lua_State* state) {
    if(!lua_isinteger(state,1)) return luaL_error(state,"camera FOV requires an integer percentage (100 for Original)");
    const auto percent=lua_tointeger(state,1);
    if(percent<50 || percent>150) return luaL_error(state,"camera FOV must be 50-150 percent (100 for Original)");
    context(state).requested_fov=static_cast<unsigned>(percent);return 0;
}
int message(lua_State* state,bool on_screen) {
    luaL_checktype(state,1,LUA_TSTRING);
    std::size_t length=0;const auto* text=lua_tolstring(state,1,&length);auto& c=context(state);
    if(!length || length>256 || c.message_count==max_messages) return luaL_error(state,"messages require 1-256 UTF-8 bytes; at most 16 messages per script");
    for(std::size_t i=0;i<length;++i) if(static_cast<unsigned char>(text[i])<32 || text[i]==127)
        return luaL_error(state,"messages must contain printable text");
    bool valid=true;
    try {static_cast<void>(wide(std::string_view(text,length)));} catch(...) {valid=false;}
    if(!valid) return luaL_error(state,"message is not valid UTF-8");
    auto& output=c.messages[c.message_count++];std::memcpy(output.text.data(),text,length);output.on_screen=on_screen;
    return 0;
}
int log(lua_State* state) {return message(state,false);}
int show_message(lua_State* state) {return message(state,true);}
int on(lua_State* state) {
    auto& c=context(state);
    if(!c.registering) return luaL_error(state,"register event handlers during startup");
    luaL_checktype(state,1,LUA_TSTRING);luaL_checktype(state,2,LUA_TFUNCTION);
    std::size_t length=0;const auto* name=lua_tolstring(state,1,&length);
    for(unsigned i=0;i<script_event_names.size();++i) if(std::string_view(name,length)==script_event_names[i]) {
        if(c.handlers[i]!=LUA_NOREF) return luaL_error(state,"only one handler per event is allowed");
        lua_pushvalue(state,2);c.handlers[i]=luaL_ref(state,LUA_REGISTRYINDEX);return 0;
    }
    return luaL_error(state,"unknown gameplay event");
}
int prepare(lua_State* state) {
    luaL_requiref(state,"_G",luaopen_base,1);lua_pop(state,1);
    luaL_requiref(state,LUA_MATHLIBNAME,luaopen_math,1);lua_pop(state,1);
    lua_newtable(state);
    // Copy only the capabilities exposed to mod code. No loaders, protected
    // calls, coroutines, OS/I/O/package/debug libraries or native userdata.
    for(const auto* name:{"assert","error","ipairs","next","pairs","select","tonumber","tostring","type","_VERSION","math"}) {
        lua_getglobal(state,name);lua_setfield(state,-2,name);
    }
    lua_pushvalue(state,-1);lua_setfield(state,-2,"_G");
    lua_newtable(state);
    lua_pushinteger(state,1);lua_setfield(state,-2,"api_version");
    lua_pushlstring(state,target::id.data(),target::id.size());lua_setfield(state,-2,"target");
    lua_pushliteral(state,"percent");lua_setfield(state,-2,"camera_fov_unit");
    constexpr luaL_Reg api[]={{"get_camera_fov",get_fov},{"set_camera_fov",set_fov},{"log",log},{"show_message",show_message},{"on",on},{nullptr,nullptr}};
    luaL_setfuncs(state,api,0);lua_setfield(state,-2,"b2");
    context(state).environment=luaL_ref(state,LUA_REGISTRYINDEX);return 0;
}
struct Evaluation {ScriptRun run;std::vector<ScriptMessage> messages;};
int invoke_event(lua_State* state) {
    const auto& c=context(state);const auto& e=c.event;const auto name=script_event_names[unsigned(e.type)];
    lua_rawgeti(state,LUA_REGISTRYINDEX,c.handlers[unsigned(e.type)]);
    // Payload allocation and calls both stay inside the protected Lua boundary.
    lua_createtable(state,0,6);lua_pushlstring(state,name.data(),name.size());lua_setfield(state,-2,"name");
    const auto integer=[&](const char* field,unsigned value) {lua_pushinteger(state,value);lua_setfield(state,-2,field);};
    if(e.player) integer("player",e.player);
    switch(e.type) {
    case ScriptEventType::race_finish:integer("position",e.position);integer("mode",e.mode);break;
    case ScriptEventType::crash_result:integer("zone",e.zone);integer("medal",e.medal);break;
    case ScriptEventType::burnout:integer("chain",e.chain);break;
    case ScriptEventType::time_attack_lap:
        integer("track",e.track);lua_pushnumber(state,e.seconds);lua_setfield(state,-2,"seconds");break;
    default:break;
    }
    lua_call(state,1,0);return 0;
}
struct Sandbox {
    Context c;
    std::string id;
    std::unique_ptr<lua_State,decltype(&lua_close)> state{nullptr,lua_close};
    Sandbox(MemoryBudget& memory,std::string_view name,unsigned initial_fov):id(name) {
        c.memory=&memory;c.current_fov=initial_fov;state.reset(lua_newstate(allocate,&c));
        if(!state) throw std::runtime_error("Cannot create the bounded Lua sandbox");
        *static_cast<Context**>(lua_getextraspace(state.get()))=&c;
    }
    void checked(int status) const {
        if(status==LUA_OK) return;
        std::size_t length=0;
        const char* text=lua_type(state.get(),-1)==LUA_TSTRING?lua_tolstring(state.get(),-1,&length):nullptr;
        std::string message=text?std::string(text,std::min<std::size_t>(length,1024)):"Lua script failed";
        for(auto& ch:message) if(static_cast<unsigned char>(ch)<32 || ch==127) ch=' ';
        try {static_cast<void>(wide(message));} catch(...) {message="Lua script returned an invalid UTF-8 error";}
        throw std::runtime_error(message);
    }
    auto begin(unsigned current_fov,unsigned instructions,std::chrono::microseconds timeout) {
        const auto now=std::chrono::steady_clock::now();c.deadline=now+timeout;
        c.current_fov=current_fov;c.limit=c.remaining=instructions;c.message_count=0;c.messages={};c.requested_fov.reset();
        lua_sethook(state.get(),budget,LUA_MASKCOUNT,hook_interval);return now;
    }
    Evaluation finish(std::chrono::steady_clock::time_point started,std::string_view event={}) const {
        if(std::chrono::steady_clock::now()>c.deadline) throw std::runtime_error("script exceeded its time limit");
        const auto elapsed=std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-started).count();
        Evaluation result{{id,c.peak,elapsed,c.requested_fov,c.subscriptions(),std::string(event)},{}};
        for(unsigned i=0;i<c.message_count;++i) result.messages.push_back({id,c.messages[i].text.data(),c.messages[i].on_screen});
        return result;
    }
    Evaluation load(std::string_view source) {
        if(source.size()>max_script_bytes) throw std::runtime_error("startup.lua exceeds 64 KiB");
        const auto started=begin(c.current_fov,instruction_limit,std::chrono::milliseconds(100));
        lua_pushcfunction(state.get(),prepare);checked(lua_pcall(state.get(),0,0,0));
        checked(luaL_loadbufferx(state.get(),source.data(),source.size(),"@startup.lua","t"));
        lua_rawgeti(state.get(),LUA_REGISTRYINDEX,c.environment);lua_setupvalue(state.get(),-2,1);
        if(std::chrono::steady_clock::now()>c.deadline) throw std::runtime_error("script exceeded its time limit");
        checked(lua_pcall(state.get(),0,0,0));c.registering=false;return finish(started);
    }
    Evaluation call(const ScriptEvent& event,unsigned current_fov) {
        const auto started=begin(current_fov,event_instruction_limit,std::chrono::milliseconds(1));c.event=event;
        lua_pushcfunction(state.get(),invoke_event);checked(lua_pcall(state.get(),0,0,0));
        return finish(started,script_event_names[unsigned(event.type)]);
    }
};
Evaluation evaluate(std::string_view source,std::string_view id,unsigned initial_fov) {
    MemoryBudget memory;Sandbox sandbox(memory,id,initial_fov);return sandbox.load(source);
}
void append(ScriptResult& result,Evaluation evaluated,std::string& fov_owner,unsigned& camera_fov) {
    if(result.messages.size()+evaluated.messages.size()>128) throw std::runtime_error("Scripts exceed 128 messages");
    if(evaluated.run.camera_fov) {
        if(!fov_owner.empty()) result.conflicts.push_back({"camera_fov_percent",fov_owner,evaluated.run.mod});
        fov_owner=evaluated.run.mod;camera_fov=*evaluated.run.camera_fov;result.camera_fov=camera_fov;
    }
    result.messages.insert(result.messages.end(),std::make_move_iterator(evaluated.messages.begin()),std::make_move_iterator(evaluated.messages.end()));
    result.runs.push_back(std::move(evaluated.run));
}
}
struct ModScripts::State {
    MemoryBudget memory;
    ScriptResult startup;
    std::vector<std::unique_ptr<Sandbox>> scripts;
    ScriptEvents events;
    std::optional<ScriptEvent> pending;
    std::size_t next=0;
    std::string fov_owner;
};
ModScripts::ModScripts(std::span<const ModFile> scripts,unsigned camera_fov):state_(std::make_unique<State>()) {
    if(scripts.size()>128) throw std::runtime_error("More than 128 startup scripts enabled");
    if(camera_fov<50 || camera_fov>150) throw std::runtime_error("Camera FOV must be 50-150 percent");
    std::string fov_owner;
    for(const auto& script:scripts) {
        try {
            File input(script.source);
            if(input.size()>max_script_bytes) throw std::runtime_error("startup.lua exceeds 64 KiB");
            const auto bytes=input.read(0,static_cast<std::size_t>(input.size()));
            auto sandbox=std::make_unique<Sandbox>(state_->memory,script.mod,camera_fov);
            append(state_->startup,sandbox->load({reinterpret_cast<const char*>(bytes.data()),bytes.size()}),fov_owner,camera_fov);
            if(const auto mask=sandbox->c.subscriptions()) {
                state_->events.subscriptions_|=mask;state_->scripts.push_back(std::move(sandbox));
            }
        } catch(const std::exception& error) {throw std::runtime_error("Mod "+script.mod+" startup.lua: "+error.what());}
    }
}
ModScripts::~ModScripts()=default;
const ScriptResult& ModScripts::startup() const {return state_->startup;}
ScriptEvents& ModScripts::events() {return state_->events;}
ScriptResult ModScripts::dispatch(unsigned camera_fov) {
    auto& s=*state_;ScriptResult result;result.gameplay=true;
    if(!s.pending && !s.events.count_ && !s.events.dropped_) return result;
    if(const auto dropped=std::exchange(s.events.dropped_,0U)) result.errors.push_back({"events",std::format("Gameplay event queue full; {} events dropped.",dropped)});
    const auto deadline=std::chrono::steady_clock::now()+std::chrono::milliseconds(2);
    unsigned calls=0;
    const auto finish=[&]() {
        s.events.subscriptions_=0;
        for(const auto& script:s.scripts) if(script->state) s.events.subscriptions_|=script->c.subscriptions();
        if(!s.events.subscriptions_) {s.pending.reset();s.events.count_=0;}
        return std::move(result);
    };
    while(calls<8) {
        if(!s.pending) {s.pending=s.events.pop();s.next=0;s.fov_owner.clear();if(!s.pending) break;}
        while(s.next<s.scripts.size()) {
            auto& script=*s.scripts[s.next++];
            if(!script.state || script.c.handlers[unsigned(s.pending->type)]==LUA_NOREF) continue;
            // Reserve the full per-handler allowance. Defer remaining handlers
            // rather than giving them a smaller budget because another mod ran.
            if(std::chrono::steady_clock::now()+std::chrono::milliseconds(1)>deadline || calls==8) {--s.next;return finish();}
            ++calls;
            try {append(result,script.call(*s.pending,camera_fov),s.fov_owner,camera_fov);}
            catch(const std::exception& error) {
                result.errors.push_back({script.id,std::string(script_event_names[unsigned(s.pending->type)])+": "+error.what()});
                script.state.reset(); // Disable only the failing mod for this session.
            }
        }
        s.pending.reset();
    }
    return finish();
}
ScriptResult run_startup_scripts(std::span<const ModFile> scripts,unsigned camera_fov) {return ModScripts(scripts,camera_fov).startup();}
std::string ScriptResult::report() const {
    std::string text=std::format("{{\"format\":\"b2-mod-scripts-v2\",\"phase\":{},\"lua\":{},\"api\":1,\"camera_fov_percent\":{},\"scripts\":[",json(gameplay?"gameplay":"startup"),json(LUA_RELEASE),camera_fov?std::to_string(*camera_fov):"null");
    for(unsigned i=0;i<runs.size();++i) {
        if(i) text+=',';const auto& run=runs[i];
        text+=std::format("{{\"mod\":{},\"peak_bytes\":{},\"elapsed_ms\":{:.3f},\"camera_fov_percent\":{},\"event\":{},\"handlers\":[",json(run.mod),run.peak_bytes,run.elapsed_ms,run.camera_fov?std::to_string(*run.camera_fov):"null",json(run.event));
        bool comma=false;
        for(unsigned n=0;n<script_event_names.size();++n) if(run.handlers&(1U<<n)) {if(comma) text+=',';text+=json(script_event_names[n]);comma=true;}
        text+="]}";
    }
    text+="],\"messages\":[";
    for(unsigned i=0;i<messages.size();++i) {
        if(i) text+=',';const auto& message=messages[i];
        text+=std::format("{{\"mod\":{},\"text\":{},\"on_screen\":{}}}",json(message.mod),json(message.text),message.on_screen);
    }
    text+="],\"conflicts\":[";
    for(unsigned i=0;i<conflicts.size();++i) {
        if(i) text+=',';const auto& conflict=conflicts[i];
        text+=std::format("{{\"setting\":{},\"previous\":{},\"winner\":{}}}",json(conflict.path),json(conflict.previous),json(conflict.winner));
    }
    text+="],\"errors\":[";
    for(unsigned i=0;i<errors.size();++i) {if(i) text+=',';text+="{\"mod\":"+json(errors[i].mod)+",\"message\":"+json(errors[i].text)+'}';}
    return text+"]}";
}
std::string create_script_mod(const std::filesystem::path& directory,std::string_view name) {
    create_mod(directory,name);
    std::string manifest;
    {File input(directory/"mod.cfg");const auto bytes=input.read(0,static_cast<std::size_t>(input.size()));manifest.assign(reinterpret_cast<const char*>(bytes.data()),bytes.size());}
    manifest+="type=script\nscript=startup.lua\nscript_api=1\n";
    write_text(directory/"startup.lua","b2.show_message(\"Script loaded.\")\n-- b2.set_camera_fov(125) -- percent of the original angle; 100 restores Original\nb2.on(\"race_finish\", function(event)\n    b2.show_message(\"Finished in position \" .. event.position .. \".\")\nend)\n",false);
    write_text(directory/"mod.cfg",manifest,true);std::filesystem::remove(directory/"files");
    return "{\"format\":\"b2-create-script-mod-v1\",\"directory\":"+json(path_text(directory))+",\"api\":1}";
}
std::string inspect_scripts(const std::filesystem::path& disc_path,const std::filesystem::path& root,std::span<const std::string> enabled) {
    Disc disc(disc_path);const auto executable=disc.find("default.xbe");
    if(executable.size!=target::file_size || disc.sha256(executable)!=target::sha256) throw std::runtime_error("Script inspection requires the verified supported disc");
    Mods mods(root,enabled,disc);return run_startup_scripts(mods.scripts()).report();
}
std::string check_scripting(const std::filesystem::path& directory) {
    if(std::filesystem::exists(directory)) throw std::runtime_error("Choose a fresh scripting diagnostic directory");
    std::filesystem::create_directories(directory);unsigned checks=0;
    const auto require=[&](bool pass,std::string_view name) {if(!pass) throw std::runtime_error("Scripting check failed: "+std::string(name));++checks;};
    const auto rejects=[&](std::string_view source,std::string_view message) {
        bool rejected=false;try {evaluate(source,"invalid",100);} catch(const std::exception& error) {rejected=std::string_view(error.what()).find(message)!=std::string_view::npos;}
        require(rejected,message);
    };
    const auto basic=evaluate("assert(b2.api_version==1 and b2.camera_fov_unit=='percent'); assert(b2.get_camera_fov()==100); b2.set_camera_fov(125); assert(b2.get_camera_fov()==125); b2.log('ready')", "basic",100);
    require(basic.run.camera_fov==125 && basic.messages.size()==1 && basic.messages[0].text=="ready","startup API returns staged settings and messages");
    const auto test=evaluate(test_mod_script,"modding-test",100);
    require(test.messages.size()==1 && test.messages[0].on_screen && test.messages[0].text=="Test Mod scripting is working.","Test Mod produces its visible confirmation");
    evaluate("assert(io==nil and os==nil and package==nil and debug==nil and require==nil and load==nil and loadfile==nil and dofile==nil and collectgarbage==nil and coroutine==nil and pcall==nil and xpcall==nil and string==nil); assert(math.sqrt(81)==9)","sandbox",100);++checks;
    evaluate("hidden_global=42; b2.api_version=99","private",100);
    evaluate("assert(hidden_global==nil and b2.api_version==1)","isolated",100);++checks;
    rejects("while true do end","instructions");
    rejects("local s='abcdefgh'; for i=1,30 do s=s..s end","memory");
    rejects("b2.set_camera_fov(151)","50-150");rejects("b2.set_camera_fov(75.5)","integer");
    rejects("b2.set_camera_fov(0)","50-150");rejects("b2.show_message('bad\\0text')","printable");
    rejects("error('\\255')","UTF-8");
    rejects("for i=1,17 do b2.log('message') end","16 messages");rejects("local = broken","startup.lua");
    rejects(std::string(max_script_bytes+1,' '),"64 KiB");rejects(std::string_view("\x1bLua",4),"binary");
    rejects("b2.on('tick',function() end)","unknown gameplay event");
    rejects("b2.on('race_finish',42)","function");
    rejects("b2.on('burnout',function() end); b2.on('burnout',function() end)","one handler");
    const auto root=directory/"mods";std::filesystem::create_directory(root);
    create_script_mod(root/"first","First script");create_script_mod(root/"second","Second script");
    write_text(root/"first/startup.lua","b2.set_camera_fov(125)",true);
    write_text(root/"second/startup.lua","assert(b2.get_camera_fov()==125); b2.set_camera_fov(150)",true);
    const auto found=discover_mods(root);
    require(found.size()==2 && found[0].error.empty() && found[0].script && found[0].files.empty(),"script-only mods use the existing discovery path");
    const std::array scripts{*found[0].script,*found[1].script};const auto mounted=run_startup_scripts(scripts);
    require(mounted.camera_fov==150 && mounted.conflicts.size()==1 && mounted.conflicts[0].winner=="second","last enabled script wins with setting-conflict details");
    write_text(root/"second/startup.lua","b2.set_camera_fov(150); error('failed after setting')",true);
    bool failed=false;try {run_startup_scripts(scripts);} catch(const std::exception&) {failed=true;}
    require(failed,"failed scripts abort startup without applying partial output");
    write_text(root/"first/startup.lua",test_mod_script,true);
    {
        ModScripts runtime(std::span(scripts).first(1));
        runtime.events().push({.type=ScriptEventType::race_finish,.player=1,.position=2,.mode=1});
        const auto event=runtime.dispatch(100);
        require(event.errors.empty() && event.messages.size()==1 && event.messages[0].on_screen && event.messages[0].text=="Test Mod: finished in position 2.","Test Mod reacts to a gameplay event");
        require(runtime.dispatch(100).runs.empty(),"event callbacks run once, with no per-frame polling");
    }
    write_text(root/"first/startup.lua","local count=0; b2.on('burnout',function(e) assert(io==nil and e.name=='burnout'); count=count+1; b2.show_message(count..':'..e.chain); b2.set_camera_fov(125) end)",true);
    write_text(root/"second/startup.lua","b2.on('burnout',function(e) assert(b2.get_camera_fov()==125); b2.set_camera_fov(150) end)",true);
    {
        ModScripts runtime(scripts);std::vector<ScriptMessage> messages;bool conflict=false;
        for(unsigned chain:{1U,2U}) {
            runtime.events().push({.type=ScriptEventType::burnout,.player=2,.chain=chain});unsigned fov=100;
            for(unsigned frame=0;frame<4;++frame) {
                auto event=runtime.dispatch(fov);require(event.errors.empty(),"gameplay callbacks stay sandboxed");
                if(event.camera_fov) fov=*event.camera_fov;
                messages.insert(messages.end(),event.messages.begin(),event.messages.end());conflict|=!event.conflicts.empty();
            }
            require(fov==150,"event settings follow enabled mod order");
        }
        require(messages.size()==2 && messages[0].text=="1:1" && messages[1].text=="2:2","handler state survives between gameplay events");
        require(conflict,"conflicting event settings name their providers");
    }
    for(const auto source:{"b2.on('race_finish',function() b2.set_camera_fov(125); b2.show_message('discard'); error('broken') end)",
                           "b2.on('race_finish',function() while true do end end)",
                           "b2.on('race_finish',function() local s='abcdefgh'; for i=1,30 do s=s..s end end)",
                           "b2.on('race_finish',function() b2.on('burnout',function() end) end)"}) {
        write_text(root/"first/startup.lua",source,true);
        write_text(root/"second/startup.lua","b2.on('race_finish',function() b2.show_message('healthy') end)",true);
        ModScripts runtime(scripts);runtime.events().push({.type=ScriptEventType::race_finish,.player=1,.position=1});
        unsigned failures=0,healthy=0;
        for(unsigned frame=0;frame<4;++frame) {
            const auto event=runtime.dispatch(100);failures+=unsigned(event.errors.size());
            require(!event.camera_fov,"failed callbacks discard proposed settings");
            for(const auto& message:event.messages) {require(message.text=="healthy","failed callbacks discard messages");++healthy;}
        }
        require(failures==1 && healthy==1,"failed callbacks disable only their own mod");
        runtime.events().push({.type=ScriptEventType::race_finish,.player=1,.position=1});
        const auto again=runtime.dispatch(100);
        require(again.errors.empty() && again.messages.size()==1 && again.messages[0].text=="healthy","disabled scripts are not retried during gameplay");
    }
    write_text(root/"first/startup.lua",test_mod_script,true);
    {
        ModScripts runtime(std::span(scripts).first(1));
        runtime.events().push({.type=ScriptEventType::burnout,.chain=1});
        require(runtime.dispatch(100).runs.empty(),"unsubscribed events never enter the queue");
        for(unsigned i=0;i<65;++i) runtime.events().push({.type=ScriptEventType::race_finish,.player=1,.position=1});
        unsigned errors=0,messages=0;
        for(unsigned frame=0;frame<128;++frame) {const auto event=runtime.dispatch(100);errors+=unsigned(event.errors.size());messages+=unsigned(event.messages.size());}
        require(errors==1 && messages==64,"event overflow is bounded and reported without replacing older events");
    }
    const auto report=std::format("{{\"format\":\"b2-scripting-check-v1\",\"passed\":true,\"checks\":{},\"game_booted\":false,\"memory_limit\":{},\"instruction_limit\":{},\"test_mod_message\":{},\"mount\":{}}}",checks,memory_limit,instruction_limit,json(test.messages[0].text),mounted.report());
    write_text(directory/"scripting-check.json",report+'\n',false);return report;
}
}
