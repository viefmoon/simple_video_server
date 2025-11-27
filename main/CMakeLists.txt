# Select source file based on streamer mode
if(CONFIG_STREAMER_MODE_HTTP)
    set(srcs "raw_http_streamer.c")
else()
    set(srcs "simple_video_server_example.c")
endif()

idf_component_register(SRCS "${srcs}"
                       PRIV_INCLUDE_DIRS .)
