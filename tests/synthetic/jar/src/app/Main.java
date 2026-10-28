package app;

// jvm-15 click-through fixture: the outer jar's Main-Class
public class Main {
    public static void main(String[] args) {
        System.out.println(lib.Helper.greet(args.length > 0 ? args[0] : "world"));
    }
}
