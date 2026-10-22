on run arguments
    set action to item 1 of arguments
    if action is "disc" then
        set chosenDisc to choose file with prompt "Select your US Xbox Burnout 2 XISO" default location (POSIX file (item 2 of arguments))
        return POSIX path of chosenDisc
    else if action is "confirm" then
        display dialog (item 2 of arguments) with title "Burnout 2 setup" buttons {"Cancel", "Continue"} default button "Continue" cancel button "Cancel"
    else if action is "message" then
        display dialog (item 2 of arguments) with title "Burnout 2 setup" buttons {"OK"} default button "OK"
    else if action is "xcode" then
        display dialog "Install Xcode 16.3 or newer and open it once to finish setup. Then run the Burnout 2 installer again." with title "Xcode required" buttons {"Cancel", "Get Xcode"} default button "Get Xcode" cancel button "Cancel"
        open location "https://developer.apple.com/download/all/?q=Xcode"
    end if
end run
