public class StSampleFormTriggerHandler {
    public void andFinally() {
        StSampleRecordUtility.updateSampleRecordLinks(formIdentifiers, 'Form');
    }
}

public class StSampleRecordUtility {
    public static void updateSampleRecordLinks(Map<String, Id> ids, String type) {}
}
