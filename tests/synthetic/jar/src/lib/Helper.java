package lib;

// lives in the nested lib/helper.jar
public class Helper {
    public static String greet(String who) {
        return "hello " + who;
    }

    // varargs: a call from the outer jar prints with plain arguments only when this class is found (jvm-120)
    public static String join(String... parts) {
        return String.join(",", parts);
    }
}
