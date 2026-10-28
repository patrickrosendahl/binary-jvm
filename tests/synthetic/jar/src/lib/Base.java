package lib;

// lives in the nested lib/helper.jar; app.Other overrides name() (jvm-120)
public class Base {
    public String name() {
        return "base";
    }
}
