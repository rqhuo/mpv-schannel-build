set(command "cmake.exe;-E;copy;D:/src/libx265.a;D:/dst/libx265_main10.a")
set(script "D:/a/.../x265-10bit-lib-install--impl.cmake")
set(log "D:/a/.../x265-10bit-lib-install-err.log")
set(rc 0)

if(NOT "${rc}" STREQUAL "0")
  message(SEND_ERROR "Command failed: ${rc}
   'cmake.exe' '-E' 'copy' ...")
endif()
# 真实 impl.cmake 末尾经常有这两行：
foreach(msg OUTPUT_VAR_FILE ERROR_VAR_FILE)
endforeach()
