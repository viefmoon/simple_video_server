set(srcs "")
set(include_dirs "include")
set(priv_include_dirs "private_include")

if(CONFIG_CAMERA_IMX662)
    list(APPEND srcs "imx662.c")
endif()

idf_component_register(
    SRCS ${srcs}
    INCLUDE_DIRS ${include_dirs}
    PRIV_INCLUDE_DIRS ${priv_include_dirs}
    PRIV_REQUIRES esp_driver_gpio freertos
)

# Link with esp_cam_sensor from managed_components
if(CONFIG_CAMERA_IMX662)
    idf_component_optional_requires(PUBLIC espressif__esp_cam_sensor)
    idf_component_optional_requires(PRIVATE espressif__esp_sccb_intf)
endif()

# Force linker to include the detect function (prevents dead code elimination)
if(CONFIG_CAMERA_IMX662_AUTO_DETECT)
    target_link_libraries(${COMPONENT_LIB} INTERFACE "-u imx662_detect")
endif()
